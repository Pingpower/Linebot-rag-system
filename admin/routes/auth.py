from flask import request, redirect, url_for, render_template, session, flash
from config import ADMIN_USER, ADMIN_PASS

def register_auth_routes(app):
    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if request.method == 'POST':
            if (request.form.get('username') == ADMIN_USER and
                    request.form.get('password') == ADMIN_PASS):
                session['logged_in'] = True
                return redirect(url_for('dashboard'))
            flash('帳號或密碼錯誤', 'error')
        return render_template('login.html')

    @app.route('/logout')
    def logout():
        session.clear()
        return redirect(url_for('login'))
