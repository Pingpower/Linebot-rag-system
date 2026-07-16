import os
import sys

sys.path.append('/home/pipadmin/文件/admin')
from config import sb

res = sb.table('knowledge_base').select('*').limit(2).execute()
if res.data:
    for i, entry in enumerate(res.data):
        print(f"--- Entry {i} ---")
        print("ID:", entry.get('id'))
        print("Title:", repr(entry.get('title')))
        print("Content:", repr(entry.get('content'))[:200])
        print("Tags:", repr(entry.get('tags')))
else:
    print("No data found")
