"""A persisted sending state must become replayable after its bounded lease."""
from .conftest import build_events

def test_active_delivery_lease_is_not_reset_by_reader_or_restart(outbox,clock):
 events=build_events();outbox.capture(events,stream="s",position="1");batch,keys=outbox.build_batches()[0]
 outbox.mark_delivering(keys,lease_seconds=30)
 assert outbox.pending_rows()==[]
 from dma.outbox import Outbox
 restarted=Outbox(outbox.db_path,clock=clock)
 assert restarted.pending_rows()==[]
 clock.advance(29);assert restarted.pending_rows()==[]
 clock.advance(1);assert len(restarted.pending_rows())==1
 assert restarted.build_batches()[0][0]==batch

def test_legacy_delivering_without_lease_is_replayable(outbox):
 events=build_events();outbox.capture(events,stream="s",position="1")
 with outbox.read() as c:c.execute("UPDATE outbox SET state='delivering',delivery_lease_until=NULL")
 assert len(outbox.pending_rows())==1

def test_delivered_receipt_is_not_requeued_when_lease_expires(outbox,clock):
 events=build_events();outbox.capture(events,stream="s",position="1");batch,keys=outbox.build_batches()[0];outbox.mark_delivering(keys)
 from .conftest import accept_all
 status,payload=accept_all(batch)
 for receipt in payload["receipts"]:outbox.apply_receipt(receipt,watermark=1,restore_epoch="epoch:1")
 clock.advance(31);assert outbox.pending_rows()==[]
