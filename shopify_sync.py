"""Sincroniza novedades por primera publicación y reactivaciones observadas.

IMPORTANTE: el sondeo no detecta ciclos completos de despublicación/republicación
ocurridos entre dos ejecuciones. Para garantía total, integrar los triggers.
"""
import os
import sys
import time
from datetime import datetime, timedelta, timezone
import requests

SHOP = os.environ['SHOPIFY_SHOP'].strip().removeprefix('https://').rstrip('/')
CLIENT_ID = os.environ['SHOPIFY_CLIENT_ID'].strip()
CLIENT_SECRET = os.environ['SHOPIFY_CLIENT_SECRET'].strip()
COLLECTION_ID = 'gid://shopify/Collection/' + os.environ['SHOPIFY_COLLECTION_ID'].strip()
PUBLICATION_ID = os.environ['SHOPIFY_ONLINE_STORE_PUBLICATION_ID'].strip()
API_VERSION = os.environ.get('SHOPIFY_API_VERSION', '2026-04')
NS = 'limitbass_novedades'
DAYS = int(os.environ.get('NEW_PRODUCT_DAYS', '30'))
DRY_RUN = os.environ.get('DRY_RUN', 'true').lower() != 'false'
URL = f'https://{SHOP}/admin/api/{API_VERSION}/graphql.json'


def parse_date(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def stamp(value):
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def get_token():
    r = requests.post(f'https://{SHOP}/admin/oauth/access_token', data={
        'grant_type': 'client_credentials', 'client_id': CLIENT_ID,
        'client_secret': CLIENT_SECRET}, timeout=30)
    r.raise_for_status()
    return r.json()['access_token']


class Shopify:
    def __init__(self, token):
        self.session = requests.Session()
        self.session.headers.update({'X-Shopify-Access-Token': token, 'Content-Type': 'application/json'})

    def gql(self, query, variables=None):
        for attempt in range(5):
            try:
                r = self.session.post(URL, json={'query': query, 'variables': variables or {}}, timeout=60)
                if r.status_code in (429, 500, 502, 503, 504):
                    raise requests.HTTPError(f'HTTP {r.status_code}', response=r)
                r.raise_for_status()
                body = r.json()
                if body.get('errors'):
                    raise RuntimeError(str(body['errors']))
                return body['data']
            except (requests.RequestException, ValueError) as exc:
                if attempt == 4:
                    raise
                print(f'Error temporal: {exc}; reintento {attempt+1}/4')
                time.sleep(min(2 ** attempt * 2, 20))


def get_products(api):
    query = '''query Products($after: String, $pub: ID!) {
      products(first: 100, after: $after) {
        nodes { id createdAt status publishedOnPublication(publicationId: $pub)
          last: metafield(namespace: "limitbass_novedades", key: "desde") { value }
          seen: metafield(namespace: "limitbass_novedades", key: "publicado") { value }
        }
        pageInfo { hasNextPage endCursor }
      }
    }'''
    products = []
    cursor = None
    while True:
        page = api.gql(query, {'after': cursor, 'pub': PUBLICATION_ID})['products']
        products.extend(page['nodes'])
        if not page['pageInfo']['hasNextPage']:
            return products
        cursor = page['pageInfo']['endCursor']


def get_collection(api):
    query = '''query Collection($id: ID!, $after: String) {
      collection(id: $id) { title sortOrder ruleSet { appliedDisjunctively }
        products(first: 250, after: $after, sortKey: COLLECTION_DEFAULT) {
        nodes { id } pageInfo { hasNextPage endCursor }
      } }
    }'''
    ids = []
    cursor = None
    while True:
        col = api.gql(query, {'id': COLLECTION_ID, 'after': cursor})['collection']
        if col is None:
            raise RuntimeError('Colección no encontrada')
        if col['ruleSet'] is not None:
            raise RuntimeError('La colección debe ser manual para gestionar sus miembros')
        if col['sortOrder'] != 'MANUAL':
            raise RuntimeError('Configura el orden de la colección como MANUAL en Shopify antes de continuar')
        ids.extend(p['id'] for p in col['products']['nodes'])
        if not col['products']['pageInfo']['hasNextPage']:
            return col['title'], ids
        cursor = col['products']['pageInfo']['endCursor']


def chunks(items, n):
    for i in range(0, len(items), n):
        yield items[i:i+n]


def checked(payload, operation):
    if payload['userErrors']:
        raise RuntimeError(f'{operation}: {payload["userErrors"]}')
    return payload


def set_metafields(api, fields):
    mutation = '''mutation Set($input: [MetafieldsSetInput!]!) {
      metafieldsSet(metafields: $input) { userErrors { field message } }
    }'''
    for batch in chunks(fields, 25):
        checked(api.gql(mutation, {'input': batch})['metafieldsSet'], 'metafieldsSet')


def wait_job(api, job):
    if not job or job['done']:
        return
    for _ in range(90):
        result = api.gql('query Job($id: ID!) { job(id: $id) { done } }', {'id': job['id']})['job']
        if result and result['done']:
            return
        time.sleep(3)
    raise TimeoutError('La tarea de Shopify no ha finalizado')


def change_members(api, name, ids):
    if not ids:
        return
    if name == 'collectionRemoveProducts':
        mutation = '''mutation Change($id: ID!, $ids: [ID!]!) {
          collectionRemoveProducts(id: $id, productIds: $ids) {
            job { id done } userErrors { field message }
          }
        }'''
    else:
        mutation = '''mutation Change($id: ID!, $ids: [ID!]!) {
          collectionAddProducts(id: $id, productIds: $ids) {
            userErrors { field message }
          }
        }'''
    for batch in chunks(ids, 100):
        result = checked(api.gql(mutation, {'id': COLLECTION_ID, 'ids': batch})[name], name)
        if name == 'collectionRemoveProducts':
            wait_job(api, result.get('job'))


def reorder(api, current, desired):
    mutation = '''mutation Reorder($id: ID!, $moves: [MoveInput!]!) {
      collectionReorderProducts(id: $id, moves: $moves) {
        job { id done } userErrors { field message }
      }
    }'''
    for position, pid in enumerate(desired):
        if current[position] == pid:
            continue
        result = checked(api.gql(mutation, {'id': COLLECTION_ID, 'moves': [
            {'id': pid, 'newPosition': str(position)}
        ]})['collectionReorderProducts'], 'collectionReorderProducts')
        wait_job(api, result.get('job'))
        current.remove(pid)
        current.insert(position, pid)


def plan(products, now):
    cutoff = now - timedelta(days=DAYS)
    desired, fields = {}, []
    for product in products:
        published = product['status'] == 'ACTIVE' and product['publishedOnPublication']
        previous = product['seen']['value'] if product['seen'] else None
        previous_date = parse_date(product['last']['value']) if product['last'] else None
        if published:
            # En la primera ejecución no se inventan reactivaciones históricas.
            # Tras observar una despublicación, la próxima publicación reinicia el plazo.
            date = now if previous == 'false' else (previous_date or parse_date(product['createdAt']))
            if date >= cutoff:
                desired[product['id']] = date
            if previous_date != date:
                fields.append({
                    'ownerId': product['id'], 'namespace': NS, 'key': 'desde',
                    'type': 'date_time', 'value': stamp(date),
                })
        new_state = 'true' if published else 'false'
        if previous != new_state:
            fields.append({
                'ownerId': product['id'], 'namespace': NS, 'key': 'publicado',
                'type': 'single_line_text_field', 'value': new_state,
            })
    ordered = sorted(desired, key=lambda pid: (desired[pid], pid), reverse=True)
    return ordered, fields


def main():
    api = Shopify(get_token())
    now = datetime.now(timezone.utc)
    title, current = get_collection(api)
    products = get_products(api)
    desired, fields = plan(products, now)
    current_set, desired_set = set(current), set(desired)
    add = [pid for pid in desired if pid not in current_set]
    remove = [pid for pid in current if pid not in desired_set]
    print(f'Colección: {title} ({COLLECTION_ID})')
    print(f'Productos: {len(products)}; añadir: {len(add)}; retirar: {len(remove)}; '
          f'metafields: {len(fields)}; simulación: {DRY_RUN}')
    if DRY_RUN:
        print('SIMULACIÓN: no se ha modificado Shopify')
        return
    # Registrar estados antes de cambiar la colección. Si falla la colección,
    # la siguiente ejecución vuelve a intentar conciliar sus miembros.
    if fields:
        set_metafields(api, fields)
    change_members(api, 'collectionRemoveProducts', remove)
    change_members(api, 'collectionAddProducts', add)
    _, actual = get_collection(api)
    if set(actual) != desired_set:
        raise RuntimeError('Los miembros de la colección no coinciden tras añadir/retirar')
    if actual != desired:
        reorder(api, actual, desired)
    _, verified = get_collection(api)
    if verified != desired:
        raise RuntimeError('El orden final de la colección no coincide con el esperado')
    print('Sincronización verificada')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        sys.exit(1)
