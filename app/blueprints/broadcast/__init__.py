"""
Mecha-School — Legacy announcement history.

Parent communication now happens through the Notifications module only.
Announcement rows remain for audit/history, but this blueprint no longer
creates, sends, or deletes active parent communications.
"""
from flask import Blueprint, render_template, redirect, url_for, flash
from flask_login import login_required

from app.models import Announcement
from app.utils.decorators import permission_required, get_current_school

broadcast_bp = Blueprint('broadcast', __name__,
                          template_folder='../../templates/broadcast')


@broadcast_bp.route('/')
@login_required
@permission_required('send_broadcast')
def index():
    school = get_current_school()
    school_id = school.id if school else None

    q = Announcement.query.order_by(Announcement.created_at.desc())
    if school_id:
        q = q.filter_by(school_id=school_id)
    rows = q.limit(50).all()
    return render_template('broadcast/index.html', announcements=rows)


@broadcast_bp.route('/new', methods=['GET', 'POST'])
@login_required
@permission_required('send_broadcast')
def compose():
    flash('تم إيقاف إعلانات أولياء الأمور. يرجى استخدام الإشعارات لإرسال الرسائل.', 'warning')
    return redirect(url_for('notifications.create'))


@broadcast_bp.route('/<int:ann_id>/send', methods=['POST'])
@login_required
@permission_required('send_broadcast')
def send_now(ann_id):
    """Manually trigger a draft/scheduled announcement right now."""
    Announcement.query.get_or_404(ann_id)
    flash('تم إيقاف إرسال الإعلانات. استخدم الإشعارات بدلاً من ذلك.', 'warning')
    return redirect(url_for('notifications.create'))


@broadcast_bp.route('/<int:ann_id>/delete', methods=['POST'])
@login_required
@permission_required('send_broadcast')
def delete(ann_id):
    Announcement.query.get_or_404(ann_id)
    flash('سجل الإعلانات محفوظ للأرشفة ولا يمكن حذفه من هذه الشاشة.', 'warning')
    return redirect(url_for('broadcast.index'))
