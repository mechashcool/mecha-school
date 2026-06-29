"""
Public pages blueprint — no authentication required.
Routes here are accessible to anyone without login.
"""
from flask import Blueprint, render_template

public_pages_bp = Blueprint('public_pages', __name__)


@public_pages_bp.route('/privacy')
def privacy():
    return render_template('privacy.html')
