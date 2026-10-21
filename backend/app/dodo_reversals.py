"""Durable refund/dispute review owned by FacelessForge's billing boundary.

Existing grants do not prove credit provenance across Redis and Mongo. Preserve
balances until an operator resolves the payment; never silently ignore a refund.
"""
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

REVERSAL_EVENTS = {'refund.succeeded', 'dispute.lost', 'dispute.accepted'}
REVIEW_EVENTS = {'dispute.opened','dispute.challenged','dispute.won','dispute.cancelled','dispute.expired'}

async def handle_reversal(db, redis_client, event, data, event_id):
    payment_id = data.get('payment_id') if isinstance(data.get('payment_id'), str) else None
    now = datetime.now(timezone.utc)
    # Event identity is the document key, so retries cannot create duplicate reviews.
    result = await db.ff_billing_reviews.update_one(
        {'_id':event_id},
        {'$setOnInsert':{'webhook_id':event_id,'event':event,'payment_id':payment_id,
          'refund_id':data.get('refund_id'),'dispute_id':data.get('dispute_id'),
          'amount':data.get('amount',data.get('refund_amount')),'currency':data.get('currency'),
          'subscription_id':data.get('subscription_id'),
          'status':'needs_review','reason':'credit_provenance_and_access_review_required',
          'created_at':now}}, upsert=True)
    if result.upserted_id is not None:
        logger.warning("Dodo billing review requires action event=%s payment_id=%s webhook_id=%s", event, payment_id, event_id)
    return {'status':'needs_review','event':event,'webhook_id':event_id}
