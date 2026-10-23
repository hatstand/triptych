import restate

from triptych.services.ingest import ingest
from triptych.services.llm_service import llm_service
from triptych.services.trip import trip
from triptych.services.user import user

app = restate.app(services=[ingest, llm_service, trip, user])
