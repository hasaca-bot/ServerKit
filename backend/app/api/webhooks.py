"""Pull-request webhook handling for PR Preview Environments.

The existing push/auto-deploy webhook lives in ``app/api/git.py`` +
``app/services/webhook_service.py`` and is left untouched. This blueprint adds a
``pull_request`` endpoint that, for a webhook bound to an application with PR
previews enabled, enqueues the right preview job:

  * ``opened`` / ``reopened``     → ``preview.create``
  * ``synchronize`` / ``edited``  → ``preview.sync`` (redeploy on new commits)
  * ``closed``                    → ``preview.destroy``

Signature verification reuses :meth:`WebhookService._verify_signature` so the
same secret as the push webhook is honored. If previews aren't enabled for the
app, the call is a no-op. Best-effort throughout — a provider that lacks Docker
or DNS still gets a clean 200.

The host mounts this blueprint; ``PreviewService.register_jobs()`` must be
called at startup for the enqueued jobs to have handlers.
"""
import logging

from flask import Blueprint, request, jsonify

logger = logging.getLogger(__name__)

webhooks_bp = Blueprint('preview_webhooks', __name__)


# (provider action) -> preview job kind. Normalized to lowercase actions.
_OPEN_ACTIONS = {'opened', 'reopened'}
_SYNC_ACTIONS = {'synchronize', 'synchronized', 'edited', 'ready_for_review'}
_CLOSE_ACTIONS = {'closed'}


def _detect_source(headers):
    """Identify the provider + pull-request signal from request headers,
    mirroring the detection in app/api/git.py."""
    if 'X-GitHub-Event' in headers:
        return ('github', headers.get('X-GitHub-Event'),
                headers.get('X-Hub-Signature-256'))
    if 'X-Gitlab-Event' in headers:
        return ('gitlab', headers.get('X-Gitlab-Event'),
                headers.get('X-Gitlab-Token'))
    if 'X-Event-Key' in headers:
        return ('bitbucket', headers.get('X-Event-Key'),
                headers.get('X-Hub-Signature'))
    return (None, None, None)


def _is_pull_request_event(source, event_type):
    if not event_type:
        return False
    et = event_type.lower()
    if source == 'github':
        return et == 'pull_request'
    if source == 'gitlab':
        return 'merge request' in et  # 'Merge Request Hook'
    if source == 'bitbucket':
        return et.startswith('pullrequest:')
    return False


def _extract_pr(source, payload):
    """Normalize a provider PR payload to
    ``{action, pr_number, branch, commit_sha, pr_title}``. Best-effort."""
    payload = payload or {}
    try:
        if source == 'github':
            pr = payload.get('pull_request') or {}
            head = pr.get('head') or {}
            return {
                'action': (payload.get('action') or '').lower(),
                'pr_number': payload.get('number') or pr.get('number'),
                'branch': head.get('ref'),
                'commit_sha': head.get('sha'),
                'pr_title': pr.get('title'),
            }
        if source == 'gitlab':
            attrs = payload.get('object_attributes') or {}
            return {
                'action': (attrs.get('action') or '').lower(),
                'pr_number': attrs.get('iid') or attrs.get('id'),
                'branch': attrs.get('source_branch'),
                'commit_sha': (attrs.get('last_commit') or {}).get('id'),
                'pr_title': attrs.get('title'),
            }
        if source == 'bitbucket':
            pr = payload.get('pullrequest') or {}
            src = (pr.get('source') or {})
            branch = (src.get('branch') or {}).get('name')
            commit = (src.get('commit') or {}).get('hash')
            return {
                'action': '',  # bitbucket encodes the action in the event key
                'pr_number': pr.get('id'),
                'branch': branch,
                'commit_sha': commit,
                'pr_title': pr.get('title'),
            }
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug('PR payload extraction failed (%s): %s', source, exc)
    return {'action': '', 'pr_number': None, 'branch': None,
            'commit_sha': None, 'pr_title': None}


def _action_to_job(source, event_type, action):
    """Map a normalized PR action to a preview job kind, or None to ignore."""
    # Bitbucket carries the action in the event key (pullrequest:created /
    # :updated / :fulfilled / :rejected) rather than an `action` field.
    if source == 'bitbucket' and event_type:
        key = event_type.lower()
        if key == 'pullrequest:created':
            return 'preview.create'
        if key == 'pullrequest:updated':
            return 'preview.sync'
        if key in ('pullrequest:fulfilled', 'pullrequest:rejected'):
            return 'preview.destroy'
        return None

    act = (action or '').lower()
    if act in _CLOSE_ACTIONS or act in ('merge', 'merged', 'close'):
        return 'preview.destroy'
    if act in _OPEN_ACTIONS or act in ('open',):
        return 'preview.create'
    if act in _SYNC_ACTIONS or act in ('update', 'updated'):
        return 'preview.sync'
    return None


@webhooks_bp.route('/pull-request/<token>', methods=['POST'])
def receive_pull_request(token):
    """Receive a pull_request webhook and drive PR preview environments.

    Public endpoint (no JWT) — authenticated by the webhook signature, exactly
    like the push webhook. Always returns 200 with an ``action`` descriptor so a
    misconfigured-but-harmless delivery never trips the provider's retry logic.
    """
    from app.models import GitWebhook
    from app.services.webhook_service import WebhookService

    source, event_type, signature = _detect_source(request.headers)
    if not source:
        return jsonify({'error': 'Unknown webhook source'}), 400

    webhook = GitWebhook.query.filter_by(webhook_token=token).first()
    if not webhook:
        return jsonify({'error': 'Unknown webhook'}), 404
    if not webhook.is_active:
        return jsonify({'success': True, 'action': 'ignored',
                        'reason': 'webhook inactive'}), 200

    # Reuse the push webhook's signature verification (same secret).
    if not WebhookService._verify_signature(webhook, source, signature, request.get_data()):
        return jsonify({'error': 'Invalid signature'}), 400

    if not _is_pull_request_event(source, event_type):
        return jsonify({'success': True, 'action': 'ignored',
                        'reason': f'not a pull_request event ({event_type})'}), 200

    if not webhook.app_id:
        return jsonify({'success': True, 'action': 'ignored',
                        'reason': 'webhook not bound to an application'}), 200

    # No-op unless previews are enabled for this application.
    try:
        from app.services.preview_service import PreviewService
        settings = PreviewService.get_settings(webhook.app_id)
        if not getattr(settings, 'enabled', False):
            return jsonify({'success': True, 'action': 'ignored',
                            'reason': 'previews disabled for app'}), 200
    except Exception as exc:  # noqa: BLE001 - reported below, answered in-band
        # A failing inbound hook gets disabled by the git provider, so the
        # answer must stay 200 — but the crash still goes on the record
        # instead of hiding in a debug log.
        from app.error_reporting import record_unexpected
        record_unexpected(exc)
        return jsonify({'success': True, 'action': 'ignored',
                        'reason': 'previews unavailable'}), 200

    pr = _extract_pr(source, request.get_json(silent=True))
    job_kind = _action_to_job(source, event_type, pr.get('action'))
    if not job_kind:
        return jsonify({'success': True, 'action': 'ignored',
                        'reason': f"unhandled PR action ({pr.get('action')})"}), 200

    if job_kind == 'preview.create':
        from app.services.deploy_control_service import has_skip_preview_marker
        if has_skip_preview_marker(pr.get('pr_title')):
            return jsonify({'success': True, 'action': 'skipped',
                            'reason': 'PR title contains a skip marker ([skip preview])',
                            'pr_number': pr.get('pr_number')}), 200

    payload = {'application_id': webhook.app_id, 'pr': {
        'pr_number': pr.get('pr_number'),
        'branch': pr.get('branch'),
        'commit_sha': pr.get('commit_sha'),
        'pr_title': pr.get('pr_title'),
    }}

    # On close we destroy by preview-id (the PR number maps to exactly one
    # preview row); resolving it here avoids a full sync that — with no
    # connected provider — would otherwise tear down every preview.
    if job_kind == 'preview.destroy':
        from app.models.application_preview import ApplicationPreview
        preview = ApplicationPreview.query.filter_by(
            application_id=webhook.app_id, pr_number=pr.get('pr_number')).first()
        if not preview:
            return jsonify({'success': True, 'action': 'ignored',
                            'reason': 'no preview for closed PR'}), 200
        payload = {'preview_id': preview.id}

    # Enqueue the matching preview job. Under ENV=testing the job system is a
    # no-op; never let an enqueue failure break the webhook response.
    enqueued = False
    try:
        from app.plugins_sdk import jobs
        jobs.enqueue(job_kind, payload)
        enqueued = True
    except Exception as exc:
        logger.warning('failed to enqueue %s for app %s: %s',
                       job_kind, webhook.app_id, exc)

    return jsonify({'success': True, 'action': job_kind, 'enqueued': enqueued,
                    'pr_number': pr.get('pr_number')}), 200
