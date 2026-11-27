"""Recalculate the historical bestseller test collection."""
from shopify_sync import (
    COLLECTION_ID, DRY_RUN, Shopify, change_members, checked, get_collection,
    get_token, wait_job,
)

TARGET = "gid://shopify/Collection/700255142263"
SIZE = 24
REPORT = (
    "FROM sales SHOW net_items_sold GROUP BY product_id "
    "SINCE -90d UNTIL yesterday ORDER BY net_items_sold DESC, product_id ASC LIMIT 250"
)


def ranked_products(api):
    report = api.gql("""
        query Ranking($query: String!) {
          shopifyqlQuery(query: $query) { parseErrors tableData { rows } }
        }
    """, {"query": REPORT})["shopifyqlQuery"]
    if report["parseErrors"] or not report["tableData"]:
        raise RuntimeError(f"No se pudo leer el histórico: {report['parseErrors']}")
    ranked = [
        f"gid://shopify/Product/{row['product_id']}"
        for row in report["tableData"]["rows"]
        if str(row.get("product_id", "")).isdigit() and int(row["net_items_sold"]) > 0
    ]
    eligible = []
    for start in range(0, len(ranked), 50):
        batch = ranked[start:start + 50]
        nodes = api.gql("""
            query Eligibility($ids: [ID!]!) {
              nodes(ids: $ids) { ... on Product { id status onlineStoreUrl } }
            }
        """, {"ids": batch})["nodes"]
        online = {p["id"] for p in nodes if p and p["status"] == "ACTIVE" and p["onlineStoreUrl"]}
        eligible.extend(pid for pid in batch if pid in online)
        if len(eligible) >= SIZE:
            return eligible[:SIZE]
    raise RuntimeError(f"Solo hay {len(eligible)} candidatos elegibles; se conserva la colección.")


def reorder_ranked(api, current, desired):
    order = current.copy()
    moves = []
    for position, pid in enumerate(desired):
        if order[position] != pid:
            moves.append({"id": pid, "newPosition": str(position)})
            order.remove(pid)
            order.insert(position, pid)
    if moves:
        result = api.gql("""
            mutation Reorder($id: ID!, $moves: [MoveInput!]!) {
              collectionReorderProducts(id: $id, moves: $moves) {
                job { id done } userErrors { field message }
              }
            }
        """, {"id": TARGET, "moves": moves})["collectionReorderProducts"]
        wait_job(api, checked(result, "collectionReorderProducts")["job"])


def main():
    if COLLECTION_ID != TARGET:
        raise RuntimeError("El destino debe ser la colección de prueba Top ventas histórico.")
    api = Shopify(get_token())
    try:
        desired = ranked_products(api)
    except Exception as error:
        detail = str(error).lower()
        reason = "Shopify rechazó la consulta de ventas o su respuesta no se pudo procesar."
        if "access denied" in detail or "read_reports" in detail or "protected customer" in detail:
            reason = "Shopify denegó acceso al histórico de ventas. Revisar read_reports y acceso a datos protegidos de la aplicación de GitHub."
        elif "solo hay" in detail:
            reason = "Shopify devolvió menos de 24 productos elegibles."
        print(f"::error::{reason}")
        raise
    try:
        _, current = get_collection(api)
    except Exception:
        print("::error::No se pudo leer la colección de prueba o su orden manual.")
        raise
    add = [pid for pid in desired if pid not in current]
    remove = [pid for pid in current if pid not in desired]
    print(f"Ranking: {len(desired)} productos; añadir: {len(add)}; retirar: {len(remove)}; simulación: {DRY_RUN}")
    if DRY_RUN or current == desired:
        return
    change_members(api, "collectionAddProducts", add)
    change_members(api, "collectionRemoveProducts", remove)
    _, actual = get_collection(api)
    if set(actual) != set(desired):
        raise RuntimeError("La colección no contiene los productos esperados.")
    reorder_ranked(api, actual, desired)
    if get_collection(api)[1] != desired:
        raise RuntimeError("El orden verificado no coincide con el ranking.")
    print("Sincronización verificada.")


if __name__ == "__main__":
    main()
