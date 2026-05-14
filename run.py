"""
Al-Muhandis School Management System
Entry Point — run with: python run.py
"""
import os
from app import create_app
from app.utils.seeder import register_commands

app = create_app(os.environ.get('FLASK_ENV', 'development'))
register_commands(app)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
