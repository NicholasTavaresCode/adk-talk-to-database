import json
import os
import sys
from datetime import datetime

from google.adk.plugins import BasePlugin
from google.adk.agents import BaseAgent
from google.adk.tools import BaseTool, ToolContext
from google.adk.models import LlmRequest, LlmResponse
from google.adk.agents.invocation_context import InvocationContext
from google.adk.agents.context import Context
from google.adk.events import Event
from typing import Any
from typing import Optional
from google.genai import types
from typing_extensions import override


# ANSI styles. Colors are dropped automatically when the output is not a
# terminal (piped logs, Cloud Run, pytest capture) or when NO_COLOR is set.
_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_GREY = "\033[90m"
_RED = "\033[91m"
_GREEN = "\033[92m"
_YELLOW = "\033[93m"
_BLUE = "\033[94m"
_MAGENTA = "\033[95m"
_CYAN = "\033[96m"


def _color_enabled() -> bool:
  """Whether ANSI styling should be emitted."""
  if os.environ.get("NO_COLOR"):
    return False
  if os.environ.get("FORCE_COLOR"):
    return True
  return sys.stdout.isatty()


def _style(text: str, *codes: str) -> str:
  """Wrap text in ANSI codes, or return it untouched when color is off."""
  if not codes or not _color_enabled():
    return text
  return f"{''.join(codes)}{text}{_RESET}"


class LogPlugin(BasePlugin):
  """A plugin that logs important information at each callback point.

  This plugin helps print all critical events in the console. It is not a
  replacement of existing logging in ADK. It rather helps terminal based
  debugging by showing all logs in the console, and serves as a simple demo for
  everyone to leverage when developing new plugins.

  Each callback prints a color coded header followed by aligned ``label value``
  fields, so a run reads as a sequence of scannable blocks:

      12:04:07.812  🚀 USER MESSAGE RECEIVED             [logs]
          invocation    e-8f2c1d3a
          session       s-19ab
          content       text: 'how many orders last month?'

  This plugin helps users track the invocation status by logging:
  - User messages and invocation context
  - Agent execution flow
  - LLM requests and responses
  - Tool calls with arguments and results
  - Events and final responses
  - Errors during model and tool execution

  Example:
      >>> logging_plugin = LogPlugin()
      >>> runner = Runner(
      ...     agents=[my_agent],
      ...     # ...
      ...     plugins=[logging_plugin],
      ... )
  """

  # Layout of a field line: indent, then a dim label padded to a fixed width.
  _INDENT = 4
  _LABEL_WIDTH = 14

  def __init__(
      self, name: str = "logging_plugin", log_partial_responses: bool = False
  ):
    """Initialize the logging plugin.

    Args:
      name: The name of the plugin instance.
      log_partial_responses: When True, every streamed chunk of an LLM response
        gets its own block. Off by default because chunk-per-token output
        drowns out everything else; the final aggregated response is always
        logged.
    """
    super().__init__(name)
    self.log_partial_responses = log_partial_responses

  @override
  async def on_user_message_callback(
      self,
      *,
      invocation_context: InvocationContext,
      user_message: types.Content,
  ) -> Optional[types.Content]:
    """Log user message and invocation start."""
    self._header("🚀", "USER MESSAGE RECEIVED", _CYAN)
    self._field("invocation", invocation_context.invocation_id)
    self._field("session", invocation_context.session.id)
    self._field("user", invocation_context.user_id)
    self._field("app", invocation_context.app_name)
    self._field("root agent", self._agent_name(invocation_context.agent))
    self._field("branch", invocation_context.branch)
    self._field("content", self._format_content(user_message), _BOLD)
    return None

  @override
  async def before_run_callback(
      self, *, invocation_context: InvocationContext
  ) -> Optional[types.Content]:
    """Log invocation start."""
    self._header("🏃", "INVOCATION STARTING", _CYAN)
    self._field("invocation", invocation_context.invocation_id)
    self._field("agent", self._agent_name(invocation_context.agent))
    return None

  @override
  async def on_event_callback(
      self, *, invocation_context: InvocationContext, event: Event
  ) -> Optional[Event]:
    """Log events yielded from the runner."""
    self._header("📢", "EVENT YIELDED", _BLUE)
    self._field("event", event.id)
    self._field("author", event.author)
    self._field("final", event.is_final_response())
    self._field("content", self._format_content(event.content))

    if event.get_function_calls():
      self._field(
          "calls", ", ".join(fc.name for fc in event.get_function_calls())
      )

    if event.get_function_responses():
      self._field(
          "responses",
          ", ".join(fr.name for fr in event.get_function_responses()),
      )

    if event.long_running_tool_ids:
      self._field("long running", ", ".join(event.long_running_tool_ids))

    return None

  @override
  async def after_run_callback(
      self, *, invocation_context: InvocationContext
  ) -> Optional[None]:
    """Log invocation completion."""
    self._header("✅", "INVOCATION COMPLETED", _GREEN)
    self._field("invocation", invocation_context.invocation_id)
    self._field("agent", self._agent_name(invocation_context.agent))
    return None

  @override
  async def before_agent_callback(
      self, *, agent: BaseAgent, callback_context: Context
  ) -> Optional[types.Content]:
    """Log agent execution start."""
    self._header("🤖", "AGENT STARTING", _MAGENTA)
    self._field("agent", callback_context.agent_name)
    self._field("invocation", callback_context.invocation_id)
    self._field("branch", callback_context._invocation_context.branch)
    return None

  @override
  async def after_agent_callback(
      self, *, agent: BaseAgent, callback_context: Context
  ) -> Optional[types.Content]:
    """Log agent execution completion."""
    self._header("🤖", "AGENT COMPLETED", _MAGENTA)
    self._field("agent", callback_context.agent_name)
    self._field("invocation", callback_context.invocation_id)
    return None

  @override
  async def before_model_callback(
      self, *, callback_context: Context, llm_request: LlmRequest
  ) -> Optional[LlmResponse]:
    """Log LLM request before sending to model."""
    self._header("🧠", "LLM REQUEST", _YELLOW)
    self._field("agent", callback_context.agent_name)
    self._field("model", llm_request.model or "default")

    # Log system instruction if present
    if llm_request.config and llm_request.config.system_instruction:
      self._field(
          "system",
          self._truncate(str(llm_request.config.system_instruction), 200),
      )

    # Note: Content logging removed due to type compatibility issues
    # Users can still see content in the LLM response

    # Log available tools
    if llm_request.tools_dict:
      self._field("tools", ", ".join(llm_request.tools_dict.keys()))

    return None

  @override
  async def after_model_callback(
      self, *, callback_context: Context, llm_response: LlmResponse
  ) -> Optional[LlmResponse]:
    """Log LLM response after receiving from model."""
    if llm_response.partial and not self.log_partial_responses:
      return None

    self._header("🧠", "LLM RESPONSE", _YELLOW)
    self._field("agent", callback_context.agent_name)

    if llm_response.error_code:
      self._field("error code", llm_response.error_code, _RED)
      self._field("error", llm_response.error_message, _RED)
    else:
      self._field("content", self._format_content(llm_response.content))
      if llm_response.partial:
        self._field("partial", llm_response.partial)
      if llm_response.turn_complete is not None:
        self._field("turn complete", llm_response.turn_complete)

    # Log usage metadata if available
    if llm_response.usage_metadata:
      usage = llm_response.usage_metadata
      self._field(
          "tokens",
          f"in={usage.prompt_token_count} out={usage.candidates_token_count}",
      )

    return None

  @override
  async def before_tool_callback(
      self,
      *,
      tool: BaseTool,
      tool_args: dict[str, Any],
      tool_context: ToolContext,
  ) -> Optional[dict[str, Any]]:
    """Log tool execution start."""
    self._header("🔧", f"TOOL STARTING · {tool.name}", _GREEN)
    self._field("agent", tool_context.agent_name)
    self._field("call id", tool_context.function_call_id)
    self._field("args", self._format_args(tool_args))
    return None

  @override
  async def after_tool_callback(
      self,
      *,
      tool: BaseTool,
      tool_args: dict[str, Any],
      tool_context: ToolContext,
      result: dict[str, Any],
  ) -> Optional[dict[str, Any]]:
    """Log tool execution completion."""
    self._header("🔧", f"TOOL COMPLETED · {tool.name}", _GREEN)
    self._field("agent", tool_context.agent_name)
    self._field("call id", tool_context.function_call_id)
    self._field("result", self._format_args(result))
    return None

  @override
  async def on_model_error_callback(
      self,
      *,
      callback_context: Context,
      llm_request: LlmRequest,
      error: Exception,
  ) -> Optional[LlmResponse]:
    """Log LLM error."""
    self._header("❌", "LLM ERROR", _RED)
    self._field("agent", callback_context.agent_name)
    self._field("model", llm_request.model or "default")
    self._field("error", f"{type(error).__name__}: {error}", _RED)
    return None

  @override
  async def on_tool_error_callback(
      self,
      *,
      tool: BaseTool,
      tool_args: dict[str, Any],
      tool_context: ToolContext,
      error: Exception,
  ) -> Optional[dict[str, Any]]:
    """Log tool error."""
    self._header("❌", f"TOOL ERROR · {tool.name}", _RED)
    self._field("agent", tool_context.agent_name)
    self._field("call id", tool_context.function_call_id)
    self._field("args", self._format_args(tool_args))
    self._field("error", f"{type(error).__name__}: {error}", _RED)
    return None

  def _header(self, icon: str, title: str, color: str) -> None:
    """Print the opening line of a log block, preceded by a blank line."""
    timestamp: str = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(
        f"\n{_style(timestamp, _GREY)} "
        f"{_style(f'{icon} {title}', color, _BOLD)} "
        f"{_style(f'[{self.name}]', _GREY)}"
    )

  def _field(self, label: str, value: Any, *codes: str) -> None:
    """Print one `label value` line, skipping empty values.

    Multi-line values are indented so they stay aligned under the label column.
    """
    if value is None or value == "":
      return

    padding: str = " " * (self._INDENT + self._LABEL_WIDTH + 1)
    body: str = ("\n" + padding).join(str(value).splitlines() or [""])
    print(
        f"{' ' * self._INDENT}"
        f"{_style(label.ljust(self._LABEL_WIDTH), _GREY)} "
        f"{_style(body, *codes)}"
    )

  def _log(self, message: str) -> None:
    """Print a free-form log line in the plugin's style."""
    print(f"{_style(f'[{self.name}]', _GREY)} {message}")

  def _agent_name(self, agent: Optional[BaseAgent]) -> str:
    """Best-effort agent name for contexts where the agent may be missing."""
    return getattr(agent, "name", None) or "Unknown"

  def _truncate(self, text: str, max_length: int) -> str:
    """Shorten text to max_length, marking how much was dropped."""
    text = text.strip()
    if len(text) <= max_length:
      return text
    return f"{text[:max_length].rstrip()}… (+{len(text) - max_length} chars)"

  def _format_content(
      self, content: Optional[types.Content], max_length: int = 200
  ) -> str:
    """Format content for logging, truncating if too long."""
    if not content or not content.parts:
      return "None"

    parts = []
    for part in content.parts:
      if part.text:
        parts.append(f"text: '{self._truncate(part.text, max_length)}'")
      elif part.function_call:
        parts.append(f"function_call: {part.function_call.name}")
      elif part.function_response:
        parts.append(f"function_response: {part.function_response.name}")
      elif part.code_execution_result:
        parts.append("code_execution_result")
      else:
        parts.append("other_part")

    return "\n".join(parts)

  def _format_args(self, args: dict[str, Any], max_length: int = 600) -> str:
    """Format an argument or result dict as one `key: value` line per entry.

    Values that are long or already multi-line (SQL statements, query results)
    are rendered as an indented block instead of a single unreadable line.
    """
    if not args:
      return "{}"
    if not isinstance(args, dict):
      return self._truncate(str(args), max_length)

    lines: list[str] = []
    for key, value in args.items():
      if isinstance(value, str):
        text = value
      else:
        try:
          text = json.dumps(value, default=str, ensure_ascii=False)
          # Only spread over multiple lines when it does not fit on one.
          if len(text) > 80:
            text = json.dumps(value, indent=2, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
          text = str(value)
      text = self._truncate(text, max_length)

      if "\n" in text:
        lines.append(f"{key}:")
        lines.extend(f"  {line}" for line in text.splitlines())
      else:
        lines.append(f"{key}: {text}")

    return "\n".join(lines)
