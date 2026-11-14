import json,sys
d=json.load(open(sys.argv[1],encoding='utf-8'));R=d['results'];s=d['summary']
print(f"{s['passed']}/{s['total']}", "fails:", [i for i,r in enumerate(R,1) if not r['pass']])
for i,r in enumerate(R,1):
    if not r['pass']: print(" ",i,r.get('q'),{k:v for k,v in r.get('checks',{}).items() if not v},r.get('elapsed_s'),r.get('error',''),r.get('unmatched_numbers') or '')
