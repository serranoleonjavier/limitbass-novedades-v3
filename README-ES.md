# Limit Bass Novedades v3 — PREPARACIÓN SEGURA (NO ACTIVAR AÚN)

Este proyecto parte del ZIP original facilitado por el propietario y de la versión de pruebas previa. El workflow se ejecuta cada 30 minutos, pero **DRY_RUN=true** impide cambios en Shopify.

## Estado de la solución

- Detecta productos `ACTIVE` publicados en la publicación específica de **Tienda online**.
- En el primer sondeo usa `createdAt` como fecha de novedad conservadora; no puede reconstruir reactivaciones antiguas.
- Guarda los metafields `limitbass_novedades.publicado` y `limitbass_novedades.desde` cuando se habilite escritura.
- Cuando observa el estado `false` seguido de `true`, reinicia el plazo de 30 días y reordena la colección manual para mostrar el más reciente primero.
- Retira de la colección productos despublicados o que excedan los 30 días, **sin borrar productos de la tienda**.
- La sincronización está prevista cada 30 minutos mediante GitHub Actions, con posibles retrasos del proveedor.

## Antes de probar

1. **No tocar la colección automática actual ni el handle `novedades`.** Crear una colección **manual** de pruebas con otro handle, por ejemplo `novedades-pruebas`, y elegir orden **Manual**.
2. Obtener el ID numérico de esa colección nueva y establecerlo como variable de repositorio `SHOPIFY_COLLECTION_ID` (Settings > Secrets and variables > Actions > Variables). **No reutilizar el ID 699910717815 de la colección antigua.**
3. Configurar los secretos `SHOPIFY_SHOP`, `SHOPIFY_CLIENT_ID`, `SHOPIFY_CLIENT_SECRET` y `SHOPIFY_ONLINE_STORE_PUBLICATION_ID`. No compartir credenciales ni tokens.
4. Comprobar permisos de lectura de productos/publicaciones y escritura de productos/metafields y colecciones, y que la versión de API sea compatible. Los permisos exactos se deben verificar en Shopify Dev Dashboard.
5. Lanzar manualmente Actions y revisar la salida en modo simulación. Este modo **no crea ni modifica metafields**, por lo que no permite probar la transición completa.
6. Probar primero en una tienda o colección aislada. Solo entonces cambiar `DRY_RUN` a `false` en el workflow, de forma consciente.
7. Verificar alta, agotamiento, reposición, caducidad y orden, además del historial de ejecuciones.
8. Solo después, planificar el cambio de handle de `novedades` a la colección nueva y **reasignar** las secciones «Colección destacada» del tema Rise que referencien la colección antigua por su ID. Mantener la URL pública y comprobar enlaces y redirecciones antes de cerrar la migración.

## Advertencia: ciclos de stock entre sondeos

El sondeo **NO garantiza** detectar una despublicación y republicación que ocurran entre dos revisiones. Por tanto, la detección de *todas* las reactivaciones **aún no está implementada**. Para cubrirla, integrar eventos persistentes en Shopify Flow o un receptor de webhooks, con control de transición para que una simple subida de stock no reinicie los 30 días. No añadir a ciegas una acción que ponga `desde=ahora` en cada ejecución del flujo de publicación: ese flujo puede ejecutarse incluso cuando el producto ya está publicado.

## Nota de inventario

El flujo de publicación mostrado comprueba el stock de una variante >= 1, mientras que el de despublicación usa inventario total del producto <= 0. Conviene revisar la consistencia para productos con múltiples variantes. La solución no modifica esos flujos.

## Seguridad

Este proyecto no tiene acceso a tu tienda ni modifica el tema, los flujos o las colecciones hasta que alguien lo despliegue con permisos. No poner secretos en el ZIP.
