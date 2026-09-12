"""A2A task store backed by Google Cloud Firestore.

O a2a-sdk traz `InMemoryTaskStore` e `DatabaseTaskStore` (SQLAlchemy). Nenhum
dos dois serve aqui: o primeiro perde as tasks a cada cold start — e no Cloud
Run com `--min-instances 0` isso acontece o tempo todo, além de as 5 instâncias
não enxergarem as tasks umas das outras; o segundo exigiria um Cloud SQL só
para isso. Como as sessões já moram no Firestore
(`app_utils/firestore_session.py`), as tasks do A2A moram no mesmo lugar.

Uma task do A2A é de vida longa por natureza: o cliente cria, acompanha por
polling ou streaming, e pode voltar nela depois. É estado que precisa
sobreviver ao processo que o criou.

## Contrato

Implementa `a2a.server.tasks.TaskStore` do **a2a-sdk 1.x**, onde `Task` é uma
mensagem protobuf (`a2a.types.a2a_pb2.Task`) — não um modelo Pydantic, como era
na 0.3.x. A serialização abaixo espelha `_to_orm`/`_from_orm` do
`DatabaseTaskStore`: `MessageToDict` para gravar, `ParseDict` para reconstruir.

## Layout no Firestore

    a2a_tasks/{task_id}
        owner        # dono resolvido do ServerCallContext; filtrado em toda leitura
        context_id
        last_updated
        payload      # a Task inteira, em JSON

`task_id` é a chave do documento porque é a primary key no `DatabaseTaskStore`
também; `owner` é campo, e toda leitura confere. O payload vai como **string
JSON**, não como mapa: o Firestore rejeita nomes de campo com `__` nas duas
pontas (o serviço de sessão precisa escapá-los) e proíbe array dentro de array,
e a Task tem estruturas aninhadas o bastante para esbarrar nos dois. Como a
task é sempre lida e escrita inteira, nada se perde em não indexá-la por dentro.

⚠️ Documento do Firestore tem teto de 1 MiB. `history` guarda as mensagens da
conversa, então uma task muito longa pode chegar perto — `_MAX_PAYLOAD_BYTES`
avisa no log antes de o Firestore recusar a escrita.
"""

import json
import logging
from datetime import UTC
from typing import Any

from a2a.server.context import ServerCallContext
from a2a.server.owner_resolver import OwnerResolver, resolve_user_scope
from a2a.server.tasks.task_store import TaskStore
from a2a.types import a2a_pb2
from a2a.types.a2a_pb2 import Task
from a2a.utils.constants import DEFAULT_LIST_TASKS_PAGE_SIZE
from a2a.utils.errors import InvalidParamsError
from a2a.utils.task import decode_page_token, encode_page_token
from google.cloud.firestore_v1.async_client import AsyncClient
from google.protobuf.json_format import MessageToDict, ParseDict

logger = logging.getLogger(__name__)

_MAX_PAYLOAD_BYTES = 900_000

class FirestoreTaskStore(TaskStore):
    """`TaskStore` do A2A persistido no Firestore."""

    def __init__(
        self,
        project: str | None = None,
        database: str = "(default)",
        collection: str = "a2a_tasks",
        owner_resolver: OwnerResolver = resolve_user_scope,
        list_scan_limit: int = 1000,
    ):
        """
        Args:
            project: Projeto do Firestore. `None` deixa o ADC resolver.
            database: Banco do Firestore. O mesmo das sessões.
            collection: Coleção raiz das tasks.
            owner_resolver: Extrai o dono do `ServerCallContext`. O padrão é o
                mesmo do SDK (`context.user.user_name`), que é o que isola as
                tasks de um chamador das do outro.
            list_scan_limit: Quantos documentos do dono `list()` traz antes de
                filtrar e ordenar em Python — ver a nota em `list`.
        """
        self._db = AsyncClient(project=project, database=database)
        self._collection = collection
        self._owner_resolver = owner_resolver
        self._list_scan_limit = list_scan_limit

    def _to_document(self, task: Task, owner: str) -> dict[str, Any]:
        """Converte a Task protobuf no documento que vai para o Firestore."""
        last_updated = None
        if task.HasField("status") and task.status.HasField("timestamp"):
            last_updated = task.status.timestamp.ToDatetime().replace(
                tzinfo=UTC
            )

        payload = json.dumps(MessageToDict(task), ensure_ascii=False)

        size = len(payload.encode("utf-8"))
        if size > _MAX_PAYLOAD_BYTES:
            logger.warning(
                "Task %s ocupa %d bytes, perto do limite de 1 MiB do Firestore. "
                "Provavelmente é o `history` crescendo.",
                task.id,
                size,
            )

        return {
            "owner": owner,
            "context_id": task.context_id,
            "last_updated": last_updated,
            "payload": payload,
        }

    def _from_document(self, data: dict[str, Any]) -> Task:
        """Reconstrói a Task protobuf a partir do documento."""
        task = Task()
        ParseDict(json.loads(data["payload"]), task)
        return task

    async def save(self, task: Task, context: ServerCallContext) -> None:
        """Grava ou atualiza a task do dono resolvido."""
        owner = self._owner_resolver(context)
        await self._db.collection(self._collection).document(task.id).set(
            self._to_document(task, owner)
        )
        logger.debug("Task %s do dono %s gravada.", task.id, owner)

    async def get(self, task_id: str, context: ServerCallContext) -> Task | None:
        """Lê a task por id, desde que pertença ao dono."""
        owner = self._owner_resolver(context)
        snapshot = await self._db.collection(self._collection).document(task_id).get()

        if not snapshot.exists:
            logger.debug("Task %s não existe.", task_id)
            return None

        data = snapshot.to_dict() or {}
        if data.get("owner") != owner:
            logger.warning(
                "Task %s pedida por %r, mas pertence a outro dono.", task_id, owner
            )
            return None

        return self._from_document(data)

    async def delete(self, task_id: str, context: ServerCallContext) -> None:
        """Apaga a task, desde que pertença ao dono."""
        owner = self._owner_resolver(context)
        doc_ref = self._db.collection(self._collection).document(task_id)
        snapshot = await doc_ref.get()

        if not snapshot.exists:
            logger.warning("Pedido para apagar a task inexistente %s.", task_id)
            return

        if (snapshot.to_dict() or {}).get("owner") != owner:
            logger.warning(
                "Recusado apagar a task %s: pertence a outro dono que não %r.",
                task_id,
                owner,
            )
            return

        await doc_ref.delete()
        logger.debug("Task %s do dono %s apagada.", task_id, owner)

    async def list(
        self,
        params: a2a_pb2.ListTasksRequest,
        context: ServerCallContext,
    ) -> a2a_pb2.ListTasksResponse:
        """Lista as tasks do dono, aplicando filtros, ordem e paginação.

        A consulta ao Firestore filtra **só** por `owner`, e o resto — status,
        context_id, recorte por data, ordenação, paginação — é feito em Python,
        exatamente como faz o `InMemoryTaskStore`. É de propósito: combinar
        igualdade num campo com `order_by` em outro exige índice composto no
        Firestore, e o deploy daqui é um `gcloud run deploy` sem Terraform nem
        etapa de criação de índice. O preço é ler até `list_scan_limit`
        documentos do dono por chamada.
        """
        owner = self._owner_resolver(context)

        query = self._db.collection(self._collection).where(
            "owner", "==", owner
        ).limit(self._list_scan_limit)

        tasks: list[Task] = []
        async for snapshot in query.stream():
            tasks.append(self._from_document(snapshot.to_dict() or {}))

        if len(tasks) == self._list_scan_limit:
            logger.warning(
                "list() bateu o teto de %d tasks para o dono %r; pode haver "
                "tasks fora do resultado. Aumente list_scan_limit.",
                self._list_scan_limit,
                owner,
            )

        if params.context_id:
            tasks = [t for t in tasks if t.context_id == params.context_id]
        if params.status:
            tasks = [t for t in tasks if t.status.state == params.status]
        if params.HasField("status_timestamp_after"):
            after = params.status_timestamp_after.ToJsonString()
            tasks = [
                t
                for t in tasks
                if t.HasField("status")
                and t.status.HasField("timestamp")
                and t.status.timestamp.ToJsonString() >= after
            ]

        tasks.sort(
            key=lambda t: (
                t.status.HasField("timestamp") if t.HasField("status") else False,
                t.status.timestamp.ToJsonString()
                if t.HasField("status") and t.status.HasField("timestamp")
                else "",
                t.id,
            ),
            reverse=True,
        )

        total_size = len(tasks)
        start_idx = 0
        if params.page_token:
            start_task_id = decode_page_token(params.page_token)
            for i, task in enumerate(tasks):
                if task.id == start_task_id:
                    start_idx = i
                    break
            else:
                raise InvalidParamsError(f"Invalid page token: {params.page_token}")

        page_size = params.page_size or DEFAULT_LIST_TASKS_PAGE_SIZE
        end_idx = start_idx + page_size
        next_page_token = (
            encode_page_token(tasks[end_idx].id) if end_idx < total_size else None
        )

        return a2a_pb2.ListTasksResponse(
            next_page_token=next_page_token,
            tasks=tasks[start_idx:end_idx],
            total_size=total_size,
            page_size=page_size,
        )
