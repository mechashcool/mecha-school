"""
Al-Muhandis – Audit Log Blueprint
"""
from flask import Blueprint, render_template, request
from flask_login import login_required
from app.models import AuditLog, User
from app.utils.decorators import admin_required

audit_bp = Blueprint('audit', __name__,
                      template_folder='../../templates/audit')


@audit_bp.route('/')
@login_required
@admin_required
def index():
    page    = request.args.get('page', 1, type=int)
    user_id = request.args.get('user_id', type=int)
    action  = request.args.get('action', '')

    query = AuditLog.query
    if user_id:
        query = query.filter_by(user_id=user_id)
    if action:
        query = query.filter(AuditLog.action.ilike(f'%{action}%'))

    logs  = query.order_by(AuditLog.created_at.desc()).paginate(page=page, per_page=40)
    users = User.query.order_by(User.full_name).all()

    return render_template('audit/index.html',
                           logs=logs, users=users,
                           user_id=user_id, action=action)
