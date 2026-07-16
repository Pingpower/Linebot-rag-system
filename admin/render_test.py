import os
import sys
from flask import Flask, render_template

sys.path.append('/home/pipadmin/文件/admin')
from config import sb

app = Flask(__name__, template_folder='templates')

@app.route('/')
def test():
    # 模擬撈取資料
    res = sb.table('knowledge_base').select('*').limit(2).execute()
    # 模擬 render_template
    # 這裡我們需要偽造 selected 變數以避免報錯
    selected = {'id': 'test-company'}
    companies = [{'id': 'test-company', 'name': 'Test'}]
    html = render_template('knowledge.html', entries=res.data, selected=selected, companies=companies, total_pages=1, current_page=1)
    
    # 我們印出第一個 script block 的內容
    import re
    scripts = re.findall(r'<script type="application/json">.*?</script>', html, re.DOTALL)
    for idx, s in enumerate(scripts):
        print(f"--- Script {idx} ---")
        print(s)
    
    # 也印出 openEditModal 函式
    func = re.findall(r'function openEditModal.*?\n\}', html, re.DOTALL)
    if func:
        print("--- openEditModal ---")
        print(func[0])
    return "done"

with app.test_request_context():
    test()
