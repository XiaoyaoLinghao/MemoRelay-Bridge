"""Local atomic capture is not limited to one network delivery batch."""
import pytest
from dma.ids import MAX_BATCH_EVENTS
from dma.errors import InvalidEventError
from dma.validation import validate_batch
from .conftest import build_events

def many():
 return [build_events(native_id=f"message:{i}",text=f"record {i}")[0] for i in range(MAX_BATCH_EVENTS+17)]

def test_large_local_capture_stays_atomic_and_delivery_remains_bounded(outbox):
 events=many();result=outbox.capture(events,stream="bulk",position=str(len(events)))
 assert result["inserted"]==len(events) and outbox.cursor("bulk")==str(len(events))
 batches=outbox.build_batches()
 assert len(batches)>1 and sum(len(b["events"]) for b,_ in batches)==len(events)
 assert all(len(b["events"])<=MAX_BATCH_EVENTS and validate_batch(b)==[] for b,_ in batches)
 again=outbox.capture(events,stream="bulk",position=str(len(events)))
 assert again["inserted"]==0 and again["duplicates"]==len(events)

def test_invalid_late_window_does_not_write_earlier_window_or_cursor(outbox):
 events=many();events[-1]["schema_version"]="unknown"
 with pytest.raises(InvalidEventError):outbox.capture(events,stream="bulk",position=str(len(events)))
 assert outbox.cursor("bulk") is None and outbox.stats()["total"]==0

def test_duplicate_part_across_validation_windows_is_still_rejected(outbox):
 events=many();events[-1]=events[0]
 with pytest.raises(InvalidEventError):outbox.capture(events,stream="bulk",position=str(len(events)))
 assert outbox.cursor("bulk") is None and outbox.stats()["total"]==0

def test_large_capture_crash_before_commit_rolls_back_everything(outbox):
 events=many()
 def crash():raise RuntimeError("synthetic crash before commit")
 with pytest.raises(RuntimeError):outbox.capture(events,stream="bulk",position=str(len(events)),after_insert_hook=crash)
 assert outbox.cursor("bulk") is None and outbox.stats()["total"]==0
