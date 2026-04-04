from kintree.search import tme_api
from kintree.config import config_interface, settings

settings.load_inventree_settings()
part = '7-215083-8'
conf = config_interface.load_file(settings.CONFIG_TME_API)
params = {'SymbolList[0]': part}
req = tme_api.tme_api_request('/Products/GetProductsFiles', conf, params)
resp = tme_api.tme_api_query(req)

print('resp_type', type(resp).__name__)
if isinstance(resp, dict):
    print('status', resp.get('Status'))
    print('top_keys', list(resp.keys()))
    data = resp.get('Data', {})
    print('data_keys', list(data.keys()) if isinstance(data, dict) else type(data).__name__)
    plist = data.get('ProductList', []) if isinstance(data, dict) else []
    print('product_count', len(plist))
    if plist:
        first = plist[0]
        print('first_product_keys', list(first.keys()))
        print('first_product_symbol', first.get('Symbol'))
        files = first.get('Files', {})
        print('files_keys', list(files.keys()) if isinstance(files, dict) else type(files).__name__)
        docs = files.get('DocumentList', []) if isinstance(files, dict) else []
        print('document_count', len(docs) if isinstance(docs, list) else 'n/a')
        if isinstance(docs, list):
            for doc in docs[:5]:
                print({k: doc.get(k) for k in ['DocumentType', 'Type', 'DocumentName', 'Name', 'Title', 'DocumentUrl', 'Url', 'Link']})
