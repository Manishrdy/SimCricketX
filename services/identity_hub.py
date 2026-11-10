"""Read-only projections over existing identity workflows; no new policy or writes."""
from datetime import datetime, timedelta
import json
import re

from flask import current_app, url_for
from sqlalchemy import select, func, literal, cast, String, union_all, case, or_, and_
from database import db
from database import models as m
from services import identity_review as review, identity_signup as signup, identity_network as network
from utils.identity_devices import keys

WORKFLOWS = {'review': 'Duplicate reviews', 'signup': 'Signup / return', 'network': 'Network access',
             'email': 'Email changes', 'campaign': 'Email campaign', 'merge': 'Account merges'}


def t(name):
    return db.metadata.tables[name]


def now():
    return datetime.utcnow()


def member_visible(at):
    member, files = t('identity_campaign_members'), t('identity_campaign_files')
    return or_(member.c.closed_at.is_(None), member.c.closed_at > at-timedelta(days=180),
               member.c.id.in_(select(files.c.member_id).where(files.c.cleaned_at.is_(None))))


def campaign_visible(at):
    return t('identity_email_campaigns').c.id.in_(select(t('identity_campaign_members').c.campaign_id).where(member_visible(at)))


def person_text(stable):
    u = t('users')
    return func.coalesce(select(u.c.id).where(u.c.stable_id == stable).scalar_subquery(), stable, '')


def text_join(*items):
    result = literal('')
    for item in items:
        result = result + func.coalesce(cast(item, String), '') + literal(' ')
    return result


def effective(status, expires, at, pending=('approved',)):
    return case((and_(status == 'approved', expires.is_(None)), 'unavailable'),
                (and_(status.in_(pending), expires <= at), 'expired'), else_=status)


def activity(at):
    """SQL union: filter/order/paginate before materializing any rows."""
    queries = []
    def add(workflow, key, action, occurred, actor, accounts, reason, status, target, source, condition):
        queries.append(select(literal(workflow).label('workflow'), cast(key, String).label('key'), action.label('action'),
            occurred.label('occurred'), actor.label('actor'), accounts.label('accounts'), reason.label('reason'),
            status.label('status'), cast(target, String).label('target')).select_from(source).where(condition))
    c, a = review.C, review.A
    add('review', a.c.id, a.c.action, a.c.created_at, a.c.actor_id,
        text_join(c.c.left_id, c.c.right_id, person_text(c.c.left_id), person_text(c.c.right_id)),
        a.c.reason, c.c.status, c.c.id, a.join(c, a.c.case_id == c.c.id),
        and_(review.visible(at), a.c.expires_at > at))
    p, a = signup.P, signup.A
    add('signup', a.c.id, a.c.action, a.c.created_at, a.c.actor_id, text_join(p.c.email, select(t('users').c.stable_id).where(t('users').c.id == p.c.email).scalar_subquery()), a.c.reason,
        effective(p.c.status, p.c.expires_at, at), p.c.id, a.outerjoin(p, a.c.appeal_id == p.c.id),
        and_(a.c.expires_at > at, or_(p.c.id.is_(None), signup.visible_appeal(at))))
    p, a = network.P, network.A
    add('network', a.c.id, a.c.action, a.c.created_at, a.c.actor_id,
        text_join(p.c.user_stable_id, person_text(p.c.user_stable_id)), a.c.reason,
        effective(p.c.status, p.c.expires_at, at), p.c.id, a.join(p, a.c.appeal_id == p.c.id),
        and_(a.c.expires_at > at, network.visible(at)))
    for workflow, name, actor, accounts in (
        ('email', 'identity_email_changes', 'actor_id', ('user_stable_id', 'old_email', 'new_email')),
        ('merge', 'identity_merges', 'administrator', ('source', 'survivor', 'source_address', 'survivor_address'))):
        x = t(name)
        account_text = text_join(*(x.c[k] for k in accounts))
        # Creation is an actual event; current status is labelled separately, not backdated.
        add(workflow, cast(x.c.id, String)+'-request', literal('requested'), x.c.created_at, x.c[actor], account_text,
            x.c.reason, effective(x.c.status, x.c.expires_at, at, ('pending',)), x.c.id, x, literal(True))
        # The receipt's applying actor can differ from request creator. Resolve it only after pagination.
        add(workflow, cast(x.c.id, String)+'-complete', literal('completed'), x.c.completed_at, literal(None, String), account_text,
            literal('Completed operation; open receipt for ownership evidence and impact.'), x.c.status, x.c.id, x,
            and_(x.c.status == 'completed', x.c.completed_at.is_not(None)))
    a, member, campaign = t('identity_campaign_actions'), t('identity_campaign_members'), t('identity_email_campaigns')
    add('campaign', a.c.id, a.c.action, a.c.created_at, a.c.actor,
        text_join(member.c.subject, member.c.address, person_text(member.c.subject)), a.c.reason,
        func.coalesce(member.c.status, 'campaign'), member.c.id,
        a.join(campaign, a.c.campaign_id == campaign.c.id).outerjoin(member, a.c.member_id == member.c.id),
        and_(campaign_visible(at), or_(a.c.member_id.is_(None), and_(member.c.id.is_not(None), member_visible(at)))))
    return union_all(*queries).subquery()


def filters(args, *, grants=False):
    data = {k: args.get(k, '').strip() for k in ('workflow', 'action', 'q', 'from', 'to', 'status')}
    allowed = ('signup', 'network') if grants else WORKFLOWS
    if data['workflow'] and data['workflow'] not in allowed:
        raise ValueError('Unknown workflow.')
    if len(data['action']) > 40 or len(data['q']) > 254:
        raise ValueError('Filter is too long.')
    try:
        data['page'] = int(args.get('page', 1))
        if not 1 <= data['page'] <= 100000:
            raise ValueError()
        data['start'] = datetime.strptime(data['from'], '%Y-%m-%d') if data['from'] else None
        data['end'] = datetime.strptime(data['to'], '%Y-%m-%d')+timedelta(days=1) if data['to'] else None
    except (ValueError, OverflowError):
        raise ValueError('Use valid dates (YYYY-MM-DD) and a positive page number.') from None
    if data['start'] and data['end'] and data['start'] >= data['end']:
        raise ValueError('The end date must be on or after the start date.')
    if grants and data['status'] not in ('', 'open', 'active', 'expired', 'revoked', 'used', 'rejected', 'unavailable'):
        raise ValueError('Unknown approval state.')
    return data


def filtered(query, x, f):
    if f['workflow']:
        query = query.where(x.c.workflow == f['workflow'])
    if f['q']:
        query = query.where(x.c.accounts.contains(f['q'], autoescape=True))
    if f['start']:
        query = query.where(x.c.occurred >= f['start'])
    if f['end']:
        query = query.where(x.c.occurred < f['end'])
    return query


def link(workflow, target):
    if target is None:
        return url_for('identity_campaign.overview') if workflow == 'campaign' else None
    endpoint, param = {'review': ('admin_identity.detail', 'case_id'), 'signup': ('identity_signup.detail', 'appeal_id'),
        'network': ('identity_network.detail', 'appeal_id'), 'email': ('identity_email.detail', 'change_id'),
        'merge': ('identity_merge.detail', 'mid'), 'campaign': ('identity_campaign.detail', 'member_id')}[workflow]
    return url_for(endpoint, **{param: target})


def safe_receipt(raw):
    try:
        value = json.loads(raw or '{}')
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def decorate(rows):
    # Fetch only the displayed page's receipts in at most three queries.
    receipts = {}
    for workflow, name, column, action in (
        ('email', 'identity_email_changes', 'receipt_json', 'completed'),
        ('merge', 'identity_merges', 'receipt_json', 'completed'),
        ('campaign', 'identity_campaign_members', 'receipt', 'deleted')):
        ids = {r['target'] for r in rows if r['workflow'] == workflow and r['action'] == action and r['target']}
        if ids:
            source = t(name)
            for key, raw in db.session.execute(select(source.c.id, source.c[column]).where(source.c.id.in_(ids))):
                receipts[(workflow, key)] = safe_receipt(raw)
    result = []
    for item in rows:
        r = dict(item)
        r['url'] = link(r['workflow'], r['target'])
        parts = (r['accounts'] or '').split()
        emails = list(dict.fromkeys(p for p in parts if '@' in p))
        r['account_label'] = ' · '.join(emails) if emails else (r['accounts'] or '')
        r['references'] = r['accounts'] if emails and any('@' not in p for p in parts) else None
        r['backup'] = None
        r['artifact_backup'] = None
        if r.get('action') == 'completed' and r['workflow'] in ('email', 'merge'):
            receipt = receipts.get((r['workflow'], r['target']), {})
            r['actor'] = receipt.get('actor') if isinstance(receipt.get('actor'), str) else None
            name = receipt.get('backup')
            if isinstance(name, str) and re.fullmatch(r'email-[A-Za-z0-9-]+\.sqlite3', name):
                r['backup'] = name
        if r.get('workflow') == 'campaign' and r.get('action') == 'deleted' and r['target']:
            receipt = receipts.get(('campaign', r['target']), {})
            for field, suffix in (('backup', 'sqlite3'), ('artifact_backup', 'zip')):
                name = receipt.get(field)
                if isinstance(name, str) and re.fullmatch(r'email-campaign-[A-Za-z0-9-]+\.'+suffix, name):
                    r[field] = name
        result.append(r)
    actors = {r.get('actor') for r in result if r.get('actor')}
    people = dict(db.session.execute(select(m.User.stable_id, m.User.id).where(m.User.stable_id.in_(actors))).all())
    for r in result:
        r['actor_label'] = people.get(r.get('actor'), 'Unavailable actor' if r.get('actor') else 'System / actor not retained')
    return result


def audit_rows(f, at=None):
    x = activity(at or now())
    query = filtered(select(x), x, f)
    if f['action']:
        query = query.where(x.c.action == f['action'])
    rows = db.session.execute(query.order_by(x.c.occurred.desc(), x.c.workflow, x.c.key)
                              .offset((f['page']-1)*30).limit(31)).mappings().all()
    return decorate(rows[:30]), len(rows)>30


def grants(f, *, exemptions=False):
    at = now()
    queries = []
    for workflow, p, subject, visibility in (
        ('signup', signup.P, text_join(signup.P.c.email, select(t('users').c.stable_id).where(t('users').c.id == signup.P.c.email).scalar_subquery()), and_(signup.visible_appeal(at), signup.P.c.verified_at.is_not(None))),
        ('network', network.P, text_join(network.P.c.user_stable_id, person_text(network.P.c.user_stable_id)), network.visible(at))):
        state = effective(p.c.status, p.c.expires_at, at)
        state = case((state == 'approved', 'active'), else_=state)
        query = select(literal(workflow).label('workflow'), cast(p.c.id, String).label('target'), subject.label('accounts'),
            state.label('status'), p.c.created_at.label('occurred'), p.c.expires_at.label('expires'),
            (p.c.scopes if workflow == 'signup' else literal('["network"]')).label('scopes')).where(visibility)
        if exemptions:
            query = query.where(p.c.status.in_(('approved', 'expired', 'revoked', 'used')))
        queries.append(query)
    x = union_all(*queries).subquery()
    query = filtered(select(x), x, f)
    if f['status']:
        query = query.where(x.c.status == f['status'])
    rows = db.session.execute(query.order_by(x.c.occurred.desc(), x.c.workflow, x.c.target).offset((f['page']-1)*30).limit(31)).mappings().all()
    result = []
    for row in rows[:30]:
        r = dict(row)
        r['url'] = link(r['workflow'], r['target'])
        parts = (r['accounts'] or '').split()
        emails = list(dict.fromkeys(p for p in parts if '@' in p))
        r['account_label'] = ' · '.join(emails) if emails else (r['accounts'] or '')
        r['references'] = r['accounts'] if emails and any('@' not in p for p in parts) else None
        try:
            scopes = json.loads(r['scopes'])
        except (ValueError, TypeError):
            scopes = []
        labels = {**signup.SCOPE_LABELS, 'network': 'Network rule only'}
        r['scope_labels'] = [labels[s] for s in scopes if isinstance(s, str) and s in labels] if isinstance(scopes, list) else []
        result.append(r)
    return result, len(rows)>30


def count(table, *where):
    return db.session.execute(select(func.count()).select_from(table).where(*where)).scalar_one()


def configuration():
    try:
        device = 'Enabled' if keys() else 'Disabled — no dedicated key'
    except ValueError:
        device = 'Invalid configuration — operator action required'
    try:
        signup.key()
        marker = 'Configured'
    except ValueError:
        marker = 'Missing or invalid'
    return dict(device=device, marker=marker, signup=signup.enabled(), network=network.enabled(),
                provider=bool(current_app.config.get('PROXYCHECK_API_KEY')))


def overview():
    at = now()
    email, merge = t('identity_email_changes'), t('identity_merges')
    member, campaign, files = t('identity_campaign_members'), t('identity_email_campaigns'), t('identity_campaign_files')
    cards = [
        ('Open duplicate reviews', count(review.C, review.C.c.status.in_(('open', 'investigating')), review.visible(at)), 'admin_identity.duplicates', {}),
        ('Retained blocked-attempt groups', count(signup.T, signup.T.c.expires_at > at), 'identity_signup.attempts', {}),
        ('Signup / return requests', count(signup.P, signup.P.c.status == 'open', signup.P.c.verified_at.is_not(None), signup.visible_appeal(at)), 'admin_identity.approvals', {'workflow':'signup','status':'open'}),
        ('Network appeals', count(network.P, network.P.c.status == 'open', network.visible(at)), 'admin_identity.approvals', {'workflow':'network','status':'open'}),
        ('Pending email changes', count(email, email.c.status == 'pending', email.c.expires_at > at), 'identity_email.overview', {}),
        ('Pending merges', count(merge, merge.c.status == 'pending', merge.c.expires_at > at), 'identity_merge.overview', {}),
        ('Email corrections outstanding', count(member, member.c.status == 'pending', member_visible(at)), 'identity_campaign.overview', {}),
        ('Active correction holds', count(member, member.c.status == 'pending', member.c.hold_until > at), 'identity_campaign.overview', {}),
    ]
    campaigns = db.session.execute(select(campaign).where(campaign_visible(at)).order_by(campaign.c.started_at.desc()).limit(5)).mappings().all()
    # SQLite JSON table-valued projection counts failures without loading the campaign.
    valid_notices = case((func.json_valid(member.c.notices), member.c.notices), else_='{}')
    notice = func.json_each(valid_notices).table_valued('key', 'value')
    failed_notices = count(member, member_visible(at), select(literal(1)).select_from(notice)
                           .where(notice.c.value == 'failed').correlate(member).exists())
    failures = dict(email=count(email, or_(and_(email.c.status == 'pending', email.c.expires_at > at, email.c.proof_delivery == 'failed'),
                        and_(email.c.status == 'completed', or_(email.c.old_notice_sent.is_(False), email.c.new_notice_sent.is_(False))))),
                    merge=count(merge, merge.c.status == 'pending', merge.c.expires_at > at, merge.c.delivery == 'failed'),
                    notices=failed_notices, deletion=count(member, member.c.status == 'pending', member.c.error.is_not(None)),
                    files=count(files, files.c.cleaned_at.is_(None)))
    budget = db.session.execute(select(t('identity_network_budget')).where(t('identity_network_budget').c.day == at.date().isoformat())).mappings().first()
    feed = db.session.execute(select(t('identity_relay_feed').c.imported_at).where(t('identity_relay_feed').c.id == 1)).scalar_one_or_none()
    observations = dict(db.session.execute(select(network.E.c.state, func.count()).where(network.E.c.expires_at > at).group_by(network.E.c.state)).all())
    cache = t('identity_network_cache')
    cache_counts = dict(db.session.execute(select(cache.c.state, func.count()).where(cache.c.expires_at > at).group_by(cache.c.state)).all())
    return dict(cards=cards, campaigns=campaigns, failures=failures, budget=budget, feed=feed,
                feed_fresh=bool(feed and at-timedelta(hours=48) < feed <= at), observations=observations,
                cache_counts=cache_counts, config=configuration(), at=at)


def account(stable):
    at = now()
    user = db.session.execute(select(m.User).where(m.User.stable_id == stable)).scalar_one_or_none()
    merge = t('identity_merges')
    lineage = db.session.execute(select(merge.c.id, merge.c.source, merge.c.survivor, merge.c.source_address, merge.c.survivor_address,
        merge.c.completed_at).where(merge.c.status == 'completed', or_(merge.c.source == stable, merge.c.survivor == stable))
        .order_by(merge.c.completed_at.desc()).limit(20)).mappings().all()
    if not user and not lineage:
        return None
    d = t('identity_device_sightings')
    sightings = db.session.execute(select(d.c.first_seen, d.c.last_seen, d.c.cookie_confirmed, d.c.fingerprint_status)
        .where(d.c.user_stable_id == stable, d.c.last_seen > at-timedelta(days=180), d.c.last_seen <= at,
               d.c.first_seen <= d.c.last_seen).order_by(d.c.last_seen.desc()).limit(10)).mappings().all()
    member, campaign = t('identity_campaign_members'), t('identity_email_campaigns')
    campaigns = db.session.execute(select(member.c.id, member.c.status, member.c.hold_until, member.c.error, campaign.c.deadline)
        .join(campaign, member.c.campaign_id == campaign.c.id).where(member.c.subject == stable, member_visible(at))
        .order_by(campaign.c.started_at.desc()).limit(10)).mappings().all()
    observations = db.session.execute(select(network.E.c.state, network.E.c.created_at).where(network.E.c.user_stable_id == stable,
        network.E.c.expires_at > at).order_by(network.E.c.created_at.desc()).limit(5)).mappings().all()
    return dict(account=user, stable=stable, lineage=lineage, sightings=sightings, campaigns=campaigns, observations=observations, at=at)
