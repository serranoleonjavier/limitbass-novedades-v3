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
