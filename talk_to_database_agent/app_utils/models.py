import os
from google.adk.models import Gemini
from google.genai import Client
from google.genai import types

_RETRY_OPTIONS = types.HttpRetryOptions(
    attempts=5,
    initial_delay=5.0,
    max_delay=60.0,
    exp_base=1.5,
)

GEMINI_MODEL = Gemini(model="gemini-3.6-flash", retry_options=_RETRY_OPTIONS)

GEMINI_MODEL.api_client = Client(
    vertexai=True,
    project=os.environ.get("GOOGLE_CLOUD_PROJECT"),
    location="global",
    http_options=types.HttpOptions(retry_options=_RETRY_OPTIONS),
)