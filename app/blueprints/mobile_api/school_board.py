"""
Mobile API — School Board endpoints
=====================================
Available to: parent + teacher roles (audience-filtered per role)

GET  /school/videos                        paginated video list
GET  /school/videos/featured               latest active featured video for this user
GET  /school/videos/<video_id>             single video detail + auto mark-as-read
POST /school/videos/<video_id>/read        explicitly mark video as read
GET  /school/announcements                 paginated announcement list
GET  /school/announcements/featured        latest active featured announcement
GET  /school/announcements/<ann_id>        single announcement detail + auto mark-as-read
POST /school/announcements/<ann_id>/read   explicitly mark announcement as read
GET  /school/board                         combined dashboard: featured + latest lists

Security
────────
• jwt_required() validates Bearer token; login_user() sets current_user for ORM scope.
• role_required('parent', 'teacher') blocks all other roles.
• bypass_tenant_scope=True + explicit school_id=user.school_id for deterministic scoping.
• Audience filter: parent → ['parents', 'all'], teacher → ['teachers', 'all'].
• Only is_active=True, publish_at <= now, expires_at > now (or null) content is returned.
• mark-read uses INSERT with UniqueConstraint so duplicate calls are idempotent.
"""
from datetime import datetime

from flask import g, request
from sqlalchemy.exc import IntegrityError

from app.models import db, SchoolVideo, SchoolAnnouncement, SchoolContentRead
from .utils import jwt_required, role_required, ok, err, photo_url, page_args
from . import mobile_api_bp


# ── private helpers ───────────────────────────────────────────────────────────

def _audience_values(user):
    """Return the audience column values visible to this user's role."""
    role = user.role.name if user.role else ''
    if role == 'parent':
        return ('parents', 'all')
    if role == 'teacher':
        return ('teachers', 'all')
    return ()


def _visible_videos(user):
    now       = datetime.utcnow()
    audiences = _audience_values(user)
    if not audiences:
        return SchoolVideo.query.filter(False)
    return (
        SchoolVideo.query
        .execution_options(bypass_tenant_scope=True)
        .filter_by(school_id=user.school_id, is_active=True)
        .filter(SchoolVideo.audience.in_(audiences))
        .filter((SchoolVideo.publish_at == None) | (SchoolVideo.publish_at <= now))
        .filter((SchoolVideo.expires_at == None) | (SchoolVideo.expires_at > now))
    )


def _visible_announcements(user):
    now       = datetime.utcnow()
    audiences = _audience_values(user)
    if not audiences:
        return SchoolAnnouncement.query.filter(False)
    return (
        SchoolAnnouncement.query
        .execution_options(bypass_tenant_scope=True)
        .filter_by(school_id=user.school_id, is_active=True)
        .filter(SchoolAnnouncement.audience.in_(audiences))
        .filter((SchoolAnnouncement.publish_at == None) | (SchoolAnnouncement.publish_at <= now))
        .filter((SchoolAnnouncement.expires_at == None) | (SchoolAnnouncement.expires_at > now))
    )


def _read_set(user_id, content_type, content_ids):
    """Return which of ``content_ids`` this user has already read.

    P1: scoped to the ids actually being serialised (was: every read receipt
    the user ever created — unbounded growth). Isolation unchanged: receipts
    are filtered by this user_id and content_type; the ids come from queries
    already filtered by school + audience + active/publish window.
    """
    if not content_ids:
        return set()
    rows = (
        SchoolContentRead.query
        .filter(
            SchoolContentRead.user_id == user_id,
            SchoolContentRead.content_type == content_type,
            SchoolContentRead.content_id.in_(content_ids),
        )
        .with_entities(SchoolContentRead.content_id)
        .all()
    )
    return {r[0] for r in rows}


def _mark_read(user, content_type, content_id):
    """Insert a read receipt; silently ignore if already exists."""
    try:
        db.session.add(SchoolContentRead(
            school_id=user.school_id,
            user_id=user.id,
            content_type=content_type,
            content_id=content_id,
            read_at=datetime.utcnow(),
            created_at=datetime.utcnow(),
        ))
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
    # P2: refresh this user's cached badge counts immediately.
    from .badges import invalidate_user_badges
    invalidate_user_badges(user.id)


def _fmt_dt(dt):
    """ISO-8601 string with explicit +00:00 timezone suffix, or None."""
    if not dt:
        return None
    return dt.strftime('%Y-%m-%dT%H:%M:%S') + '+00:00'


def _video_dict(v, is_read=False):
    mt = getattr(v, 'media_type', None) or 'video'
    # Video media → native CDN signed URL (Range/seek support); image → normal
    # signed/proxied URL. Thumbnails are always images.
    resolved_url   = photo_url(v.video_url, want_video=(mt == 'video'))
    resolved_thumb = photo_url(v.thumbnail_url)
    return {
        'id':            v.id,
        'title':         v.title,
        'description':   v.description,
        'media_type':    mt,
        'media_url':     resolved_url,   # canonical field for both images and videos
        'video_url':     resolved_url,   # kept for Flutter backward compatibility
        'thumbnail_url': resolved_thumb,
        'audience':      v.audience,
        'is_featured':   bool(v.is_featured),
        'is_published':  True,
        'is_read':       bool(is_read),
        'school_id':     v.school_id,
        'publish_at':    _fmt_dt(v.publish_at),
        'expires_at':    _fmt_dt(v.expires_at),
        'created_at':    _fmt_dt(v.created_at),
    }


def _ann_dict(a, is_read=False):
    return {
        'id':            a.id,
        'title':         a.title,
        'body':          a.body,
        'description':   a.body,        # alias for body — Flutter-friendly field name
        'media_url':     photo_url(a.media_url, want_video=(a.media_type == 'video')),
        'media_type':    a.media_type,
        'thumbnail_url': photo_url(a.thumbnail_url),
        'audience':      a.audience,
        'is_featured':   bool(a.is_featured),
        'is_published':  True,
        'is_read':       bool(is_read),
        'school_id':     a.school_id,
        'publish_at':    _fmt_dt(a.publish_at),
        'expires_at':    _fmt_dt(a.expires_at),
        'created_at':    _fmt_dt(a.created_at),
    }


# ── Video endpoints ───────────────────────────────────────────────────────────

@mobile_api_bp.route('/school/videos', methods=['GET'])
@jwt_required()
@role_required('parent', 'teacher')
def school_videos_list():
    user   = g.mobile_user
    limit, offset = page_args(default_limit=20, max_limit=100)

    q     = _visible_videos(user).order_by(SchoolVideo.created_at.desc())
    total = q.count()
    items = q.offset(offset).limit(limit).all()

    read_ids = _read_set(user.id, 'video', [v.id for v in items])
    return ok(
        total=total, limit=limit, offset=offset,
        videos=[_video_dict(v, v.id in read_ids) for v in items],
    )


@mobile_api_bp.route('/school/videos/featured', methods=['GET'])
@jwt_required()
@role_required('parent', 'teacher')
def school_videos_featured():
    user  = g.mobile_user
    video = (
        _visible_videos(user)
        .filter_by(is_featured=True)
        .order_by(SchoolVideo.created_at.desc())
        .first()
    )
    if not video:
        return ok(video=None)
    read_ids = _read_set(user.id, 'video', [video.id])
    return ok(video=_video_dict(video, video.id in read_ids))


@mobile_api_bp.route('/school/videos/<int:video_id>', methods=['GET'])
@jwt_required()
@role_required('parent', 'teacher')
def school_video_detail(video_id):
    user  = g.mobile_user
    video = _visible_videos(user).filter_by(id=video_id).first()
    if not video:
        return err('video_not_found', 404)
    _mark_read(user, 'video', video_id)
    return ok(video=_video_dict(video, is_read=True))


@mobile_api_bp.route('/school/videos/<int:video_id>/read', methods=['POST'])
@jwt_required()
@role_required('parent', 'teacher')
def school_video_mark_read(video_id):
    user  = g.mobile_user
    video = _visible_videos(user).filter_by(id=video_id).first()
    if not video:
        return err('video_not_found', 404)
    _mark_read(user, 'video', video_id)
    return ok(message='video_marked_read')


# ── Announcement endpoints ────────────────────────────────────────────────────

@mobile_api_bp.route('/school/announcements', methods=['GET'])
@jwt_required()
@role_required('parent', 'teacher')
def school_announcements_list():
    user   = g.mobile_user
    limit, offset = page_args(default_limit=20, max_limit=100)

    q     = _visible_announcements(user).order_by(SchoolAnnouncement.created_at.desc())
    total = q.count()
    items = q.offset(offset).limit(limit).all()

    read_ids = _read_set(user.id, 'announcement', [a.id for a in items])
    return ok(
        total=total, limit=limit, offset=offset,
        announcements=[_ann_dict(a, a.id in read_ids) for a in items],
    )


@mobile_api_bp.route('/school/announcements/featured', methods=['GET'])
@jwt_required()
@role_required('parent', 'teacher')
def school_announcements_featured():
    user = g.mobile_user
    ann  = (
        _visible_announcements(user)
        .filter_by(is_featured=True)
        .order_by(SchoolAnnouncement.created_at.desc())
        .first()
    )
    if not ann:
        return ok(announcement=None)
    read_ids = _read_set(user.id, 'announcement', [ann.id])
    return ok(announcement=_ann_dict(ann, ann.id in read_ids))


@mobile_api_bp.route('/school/announcements/<int:ann_id>', methods=['GET'])
@jwt_required()
@role_required('parent', 'teacher')
def school_announcement_detail(ann_id):
    user = g.mobile_user
    ann  = _visible_announcements(user).filter_by(id=ann_id).first()
    if not ann:
        return err('announcement_not_found', 404)
    _mark_read(user, 'announcement', ann_id)
    return ok(announcement=_ann_dict(ann, is_read=True))


@mobile_api_bp.route('/school/announcements/<int:ann_id>/read', methods=['POST'])
@jwt_required()
@role_required('parent', 'teacher')
def school_announcement_mark_read(ann_id):
    user = g.mobile_user
    ann  = _visible_announcements(user).filter_by(id=ann_id).first()
    if not ann:
        return err('announcement_not_found', 404)
    _mark_read(user, 'announcement', ann_id)
    return ok(message='announcement_marked_read')


# ── Combined board endpoint ───────────────────────────────────────────────────

@mobile_api_bp.route('/school/board', methods=['GET'])
@jwt_required()
@role_required('parent', 'teacher')
def school_board():
    """Return featured items + latest 5 videos and announcements in one request."""
    user = g.mobile_user

    featured_video = (
        _visible_videos(user)
        .filter_by(is_featured=True)
        .order_by(SchoolVideo.created_at.desc())
        .first()
    )
    featured_ann = (
        _visible_announcements(user)
        .filter_by(is_featured=True)
        .order_by(SchoolAnnouncement.created_at.desc())
        .first()
    )
    videos = (
        _visible_videos(user)
        .order_by(SchoolVideo.created_at.desc())
        .limit(5).all()
    )
    announcements = (
        _visible_announcements(user)
        .order_by(SchoolAnnouncement.created_at.desc())
        .limit(5).all()
    )

    video_ids = [v.id for v in videos]
    if featured_video:
        video_ids.append(featured_video.id)
    ann_ids = [a.id for a in announcements]
    if featured_ann:
        ann_ids.append(featured_ann.id)
    read_video_ids = _read_set(user.id, 'video', video_ids)
    read_ann_ids   = _read_set(user.id, 'announcement', ann_ids)

    return ok(
        featured_video=(
            _video_dict(featured_video, featured_video.id in read_video_ids)
            if featured_video else None
        ),
        featured_announcement=(
            _ann_dict(featured_ann, featured_ann.id in read_ann_ids)
            if featured_ann else None
        ),
        videos=[_video_dict(v, v.id in read_video_ids) for v in videos],
        announcements=[_ann_dict(a, a.id in read_ann_ids) for a in announcements],
    )
