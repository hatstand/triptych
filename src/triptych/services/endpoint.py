import restate

from triptych.services.assistant import assistant
from triptych.services.eval_share import eval_share
from triptych.services.examples import examples
from triptych.services.ingest import ingest
from triptych.services.llm_service import llm_service
from triptych.services.place_photo import place_photo
from triptych.services.reminders import reminders
from triptych.services.reprocess import reprocess
from triptych.services.user import user

app = restate.app(
    services=[assistant, eval_share, examples, ingest, llm_service, place_photo, reminders, reprocess, user]
)
