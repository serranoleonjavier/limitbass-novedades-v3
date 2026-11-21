"""Crea una colección manual de pruebas en Shopify una sola vez.

Ejecución intencional: requiere CONFIRM_CREATE_COLLECTION=YES.
No altera colecciones existentes ni añade productos.
"""
import os
import sys
import requests

TITLE = 'Novedades V3 Test'

def main():
    if os.getenv('CONFIRM_CREATE_COLLECTION') != 'YES':
        raise RuntimeError('Creación bloqueada: falta CONFIRM_CREATE_COLLECTION=YES')
    shop = os.environ['SHOPIFY_SHOP'].strip().removeprefix('https://').rstrip('/')
    client_id = os.environ['SHOPIFY_CLIENT_ID'].strip()
    client_secret = os.environ['SHOPIFY_CLIENT_SECRET'].strip()
    version = os.getenv('SHOPIFY_API_VERSION', '2026-10')
    token_response = requests.post(
        f'https://{shop}/admin/oauth/access_token',
        data={'grant_type': 'client_credentials', 'client_id': client_id,
              'client_secret': client_secret}, timeout=30)
    token_response.raise_for_status()
    token = token_response.json()['access_token']
    endpoint = f'https://{shop}/admin/api/{version}/graphql.json'
    headers = {'X-Shopify-Access-Token': token, 'Content-Type': 'application/json'}

    def graphql(query, variables=None):
        response = requests.post(endpoint, headers=headers,
                                 json={'query': query, 'variables': variables or {}}, timeout=60)
        response.raise_for_status()
        data = response.json()
        if data.get('errors'):
            raise RuntimeError(f'Errores GraphQL: {data["errors"]}')
        return data['data']

    # No crear duplicados si el workflow se ejecuta accidentalmente dos veces.
    query = '''query($search: String!) {
      collections(first: 50, query: $search) {
        nodes { id title sortOrder ruleSet { appliedDisjunctively } }
      }
    }'''
    found = graphql(query, {'search': f'title:{TITLE}'})['collections']['nodes']
    matches = [c for c in found if c['title'].casefold() == TITLE.casefold()]
    if matches:
        for collection in matches:
            print(f'YA EXISTE: {collection["title"]} — {collection["id"]}')
        raise RuntimeError('Ya existe una colección con ese título. No se ha creado otra.')

    mutation = '''mutation($input: CollectionInput!) {
      collectionCreate(input: $input) {
        collection { id title sortOrder ruleSet { appliedDisjunctively } }
        userErrors { field message }
      }
    }'''
    result = graphql(mutation, {'input': {'title': TITLE, 'sortOrder': 'MANUAL'}})['collectionCreate']
    if result['userErrors']:
        raise RuntimeError(f'No se pudo crear la colección: {result["userErrors"]}')
    collection = result['collection']
    if not collection or collection['ruleSet'] is not None or collection['sortOrder'] != 'MANUAL':
        raise RuntimeError(f'La colección creada requiere revisión: {collection}')
    print('COLECCIÓN MANUAL CREADA CORRECTAMENTE')
    print('Título:', collection['title'])
    print('ID completo:', collection['id'])
    print('ID numérico para SHOPIFY_COLLECTION_ID:', collection['id'].rsplit('/', 1)[-1])
    print('No se han añadido productos ni modificado otras colecciones.')

if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        sys.exit(1)
