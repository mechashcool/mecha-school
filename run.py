"""
Al-Muhandis School Management System
Entry Point — run with: python run.py
"""
import os
from app import create_app
from app.utils.seeder import register_commands

_env = os.environ.get('FLASK_ENV', 'development')
app = create_app(_env)
register_commands(app)

if __name__ == '__main__':
    # Never enable the interactive debugger outside development — it allows
    # arbitrary code execution on any unhandled exception.
    app.run(host='0.0.0.0', port=5000, debug=(_env == 'development'))
