"""Sincroniza novedades por primera publicación y reactivaciones observadas.

IMPORTANTE: el sondeo no detecta ciclos completos de despublicación/republicación
ocurridos entre dos ejecuciones. Para garantía total, integrar los triggers.
"""
import os
import sys
import time
from datetime import datetime, timedelta, timezone
import requests

SHOP = os.environ['SHOPIFY_SHOP'].strip()
CLIENT_ID = os.environ['SHOPIFY_CLIENT_ID'].strip()
CLIENT_SECRET = os.environ['SHOPIFY_CLIENT_SECRET'].strip()
COLLECTION_ID = 'gid://shopify/Collection/' + os.environ['SHOPIFY_COLLECTION_ID'].strip()
PUBLICATION_ID = os.environ['SHOPIFY_ONLINE_STORE_PUBLICATION_ID'].strip()
API_VERSION = os.environ.get('SHOPIFY_API_VERSION', '2026-04')
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
      collection(id: $id) { title sortOrder products(first: 250, after: $after) {
        nodes { id } pageInfo { hasNextPage endCursor }
      } }
    }'''
    ids = []
    cursor = None
    while True:
        col = api.gql(query, {'id': COLLECTION_ID, 'after': cursor})['collection']
        if col is None:
            raise RuntimeError('Colección no encontrada')
        if col['sortOrder'] != 'MANUAL':
            raise RuntimeError('Configura el orden de la colección como MANUAL en Shopify antes de continuar')
        ids.extend(p['id'] for p in col['products']['nodes'])
        if not col['products']['pageInfo']['hasNextPage']:
            return ids
        cursor = col['products']['pageInfo']['endCursor']


def chunks(items, n):
    for i in range(0, len(items), n):
        yield items[i:i+n]


def check(payload, operation):
    if payload['userErrors']:
        raise RuntimeError(f'{operation}: {payload["userErrors"]}')
    return payload


def set_metafields(api, fields):
    mutation = '''mutation Set($input: [MetafieldsSetInput!]!) {
      metafieldsSet(metafields: $input) { userErrors { field message } }
    }'''
    for batch in chunks(fields, 25):
        check(api.gql(mutation, {'input': batch})['metafieldsSet'], 'metafieldsSet')


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
    mutation = ('''mutation Change($id: ID!, $ids: [ID!]!) {
      %s(id: $id, productIds: $ids) { job { id done } userErrors { field message } }
    }''' % name) if name == 'collectionRemoveProducts' else '''mutation Change($id: ID!, $ids: [ID!]!) {
      collectionAddProducts(id: $id, productIds: $ids) { userErrors { field message } }
    }'''
    for batch in chunks(ids, 100):
        result = check(api.gql(mutation, {'id': COLLECTION_ID, 'ids': batch})[name], name)
        if name == 'collectionRemoveProducts':
            wait_job(api, result.get('job'))


def reorder(api, current, desired):
    mutation = '''mutation Reorder($id: ID!, $moves: [MoveInput!]!) {
      collectionReorderProducts(id: $id, moves: $moves) {
        job { id done } userErrors { field message }
      }
    }'''
    # Mover de izquierda a derecha, únicamente si difiere la posición.
    for position, pid in enumerate(desired):
        if current[position] == pid:
            continue
        move = {'id': pid, 'newPosition': str(position)}
        result = check(api.gql(mutation, {'id': COLLECTION_ID, 'moves': [move]})['collectionReorderProducts'], 'reorder')
        wait_job(api, result.get('job'))
        current.remove(pid)
        current.insert(position, pid)


def plan(products, now):
    cutoff = now - timedelta(days=DAYS)
    desired = {}
    fields = []
    for p in products:
        published = p['status'] == 'ACTIVE' and p['publishedOnPublication']
        old_seen = p['seen']['value'] if p['seen'] else None
        old_date = parse_date(p['last']['value']) if p['last'] else None
        if published:
            # Inicialización conservadora: productos antiguos sin historial NO se consideran
            # repuestos. Se usa createdAt hasta observar una transición FALSE -> TRUE.
            date = now if old_seen == 'false' else (old_date or parse_date(p['createdAt']))
            if date >= cutoff:
                desired[p['id']] = date
            if old_date != date:
                fields.append({'ownerId': p['id'], 'namespace': 'limitbass_novedades',
                               'key': 'desde', 'type': 'date_time', 'value': stamp(date)})
        if old_seen != ('true' if published else 'false'):
            fields.append({'ownerId': p['id'], 'namespace': 'limitbass_novedades',
                           'key': 'publicado', 'type': 'single_line_text_field',
                           'value': 'true' if published else 'false'})
    ordered = sorted(desired, key=lambda pid: (desired[pid], pid), reverse=True)
    return ordered, fields


def main():
    api = Shopify(get_token())
    now = datetime.now(timezone.utc)
    products = get_products(api)
    current = get_collection(api)
    desired, fields = plan(products, now)
    add = [p for p in desired if p not in current]
    remove = [p for p in current if p not in desired]
    print(f'Productos: {len(products)}; añadir: {len(add)}; retirar: {len(remove)}; metafields: {len(fields)}; modo simulación: {DRY_RUN}')
    if DRY_RUN:
        print('SIMULACIÓN: sin cambios en Shopify')
        return
    if fields:
        set_metafields(api, fields)
    if remove:
        change_members(api, 'collectionRemoveProducts', remove)
    if add:
        change_members(api, 'collectionAddProducts', add)
    actual = get_collection(api)
    if set(actual) != set(desired):
        raise RuntimeError('La colección no coincide con el resultado esperado tras añadir/retirar')
    if actual != desired:
        reorder(api, actual, desired)
    if get_collection(api) != desired:
        raise RuntimeError('La colección no quedó ordenada como se esperaba')
    print('Sincronización verificada')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        sys.exit(1)
