"""Application-generated documentary work file, not a fabricated completed analysis."""
from django.db import transaction
from .document_index import search_passages
from .inquiry import prepare_inquiry
from .canonical import sha256_digest
from atelier.models import InquiryCase

@transaction.atomic
def prepare_research_case(query):
    passages=search_passages(query=query,limit=10)
    # Signed navigation cursors carry a timestamp: not part of the immutable case.
    passages['more_passages_available']=bool(passages.pop('next_cursor'))
    candidates=prepare_inquiry(query=query,limit=10)
    evidence={'passages':passages,'declarations_and_votes':candidates,
              'qualification':'needs_evidence','conclusion':None,'review_authorized':False,
              'limits':'Correspondances lexicales, pas comparaison établie ; actes et portée à qualifier.'}
    digest=sha256_digest(evidence)
    case,created=InquiryCase.objects.get_or_create(input_digest=digest,defaults={
        'query':query,'corpus_version':passages['corpus_version'],'candidate_evidence':evidence})
    return case,created
