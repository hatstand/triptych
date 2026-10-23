import restate

from triptych.services.assistant import assistant
from triptych.services.ingest import ingest
from triptych.services.llm_service import llm_service
from triptych.services.reminders import reminders
from triptych.services.user import user

app = restate.app(services=[assistant, ingest, llm_service, reminders, user])
