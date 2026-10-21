import unittest
from unittest.mock import AsyncMock, Mock
from app.dodo_reversals import handle_reversal, REVERSAL_EVENTS, REVIEW_EVENTS
class ReversalPolicy(unittest.IsolatedAsyncioTestCase):
 async def test_every_event_is_durable_review_without_credit_or_access_writes(self):
  for event in REVERSAL_EVENTS | REVIEW_EVENTS:
   db=Mock(); db.ff_billing_reviews.update_one=AsyncMock(); redis=Mock()
   result=await handle_reversal(db,redis,event,{'payment_id':'pay_1','amount':100},'event_1')
   self.assertEqual(result['status'],'needs_review')
   args,kw=db.ff_billing_reviews.update_one.call_args
   self.assertEqual(args[0],{'_id':'event_1'})
   self.assertEqual(args[1]['$setOnInsert']['payment_id'],'pay_1')
   self.assertTrue(kw['upsert'])
   self.assertEqual([c[0] for c in db.mock_calls],['ff_billing_reviews.update_one'])
   self.assertEqual(redis.mock_calls,[])
 async def test_missing_reference_still_recorded(self):
  db=Mock();db.ff_billing_reviews.update_one=AsyncMock()
  await handle_reversal(db,Mock(),'refund.succeeded',{},'event_2')
  self.assertIsNone(db.ff_billing_reviews.update_one.call_args.args[1]['$setOnInsert']['payment_id'])
 async def test_storage_failure_is_not_acknowledged(self):
  db=Mock();db.ff_billing_reviews.update_one=AsyncMock(side_effect=RuntimeError('db down'))
  with self.assertRaises(RuntimeError): await handle_reversal(db,Mock(),'refund.succeeded',{},'event_3')


class WebhookBoundary(unittest.IsolatedAsyncioTestCase):
 async def test_review_precedes_redis_grant_guard_and_duplicate_is_upsert(self):
  import os,json
  from unittest.mock import patch
  from starlette.requests import Request
  from app.billing_dodo import dodo_webhook
  db=Mock();db.ff_billing_reviews.update_one=AsyncMock();redis=Mock()
  body=json.dumps({'type':'refund.succeeded','data':{'payment_id':'pay_test','amount':100}}).encode()
  async def receive(): return {'type':'http.request','body':body,'more_body':False}
  with patch.dict(os.environ,{'DODO_PAYMENTS_WEBHOOK_KEY':'test-only'}), patch('app.dodo_webhook_verify.verify_signature'), patch('app.billing_dodo.r',redis), patch('app.db.get_db',return_value=db):
   for _ in range(2):
    result=await dodo_webhook(Request({'type':'http','headers':[]},receive),webhook_id='event-boundary',webhook_timestamp='1',webhook_signature='test')
    self.assertEqual(result['status'],'needs_review')
  self.assertEqual(redis.mock_calls,[])
  self.assertEqual(db.ff_billing_reviews.update_one.await_count,2)
