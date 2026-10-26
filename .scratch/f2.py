import sys,re,html,urllib.request
url=sys.argv[1]; n=int(sys.argv[2]) if len(sys.argv)>2 else 8000; off=int(sys.argv[3]) if len(sys.argv)>3 else 0
req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0'})
h=urllib.request.urlopen(req,timeout=60).read().decode('utf-8','ignore')
m=re.search(r'<title>(.*?)</title>',h,re.S); print('TITLE:',html.unescape(m.group(1)).strip() if m else '')
i=h.find('icms-help-docs-content')
body=h[i:i+500000] if i>0 else h
body=re.sub(r'<script.*?</script>','',body,flags=re.S)
body=re.sub(r'</(p|div|li|tr|h[1-6]|pre|table|br)>','\n',body)
body=re.sub(r'<br\s*/?>','\n',body)
body=re.sub(r'<[^>]+>','',body); body=html.unescape(body)
body=re.sub(r'[ \t]+',' ',body); body=re.sub(r'\n\s*\n+','\n',body)
print('BODYLEN:',len(body))
print(body[off:off+n])
