"""Cómo se llaman los archivos que TODOS los usuarios comparten en el servidor.

Viven en la carpeta que cada uno configura en Ajustes ("Carpeta compartida
(datos)", `shared_data_ftp_path`), que no es exclusiva de esta aplicación: por
eso los archivos llevan delante el nombre de la app, para no chocar con nada más
que haya ahí.

El nombre estaba escrito a mano en los doce sitios de `gui/app.py` que
construyen esas rutas. Al cambiar el nombre de la aplicación eso era una trampa:
renombrar once y olvidar uno habría dejado a la mitad de los datos del grupo en
un sitio y a la otra mitad en otro, sin ningún error a la vista. Ahora el nombre
está aquí y solo aquí.
"""

from core.appdirs import APP_NAME, LEGACY_APP_NAME

#: Cada archivo compartido, por su nombre corto (sin prefijo ni extensión).
#: La clave es la que se usa en el código; el valor, el nombre real en el
#: servidor -- se conservan tal cual estaban para no romper nada al renombrar.
SHARED_DATA_FILES = {
    "favoritos":                        "favoritos.json",
    "reservas":                         "reservas.json",
    "config_servidor":                  "config_servidor.json",
    "doblaje_ia":                       "doblaje_ia.json",
    "actividad":                        "actividad.json",
    "estadisticas_usuarios":            "estadisticas_usuarios.json",
    "estadisticas_categorias":          "estadisticas_categorias.json",
    "estadisticas_borrados":            "estadisticas_borrados.json",
    "estadisticas_subidores_categoria": "estadisticas_subidores_categoria.json",
    "liberar_espacio":                  "liberar_espacio.json",
    "episodios_que_faltan":             "episodios_que_faltan.json",
    "peliculas":                        "peliculas.json",
}


def filename(key: str, legacy: bool = False) -> str:
    """Nombre del archivo compartido *key* en el servidor.

    Con *legacy* se obtiene el nombre que tenía cuando la aplicación se llamaba
    de otra forma, que es lo que hay que leer para traerse los datos ya
    existentes (ver la migración en gui/app.py)."""
    prefijo = LEGACY_APP_NAME if legacy else APP_NAME
    return f"{prefijo}_{SHARED_DATA_FILES[key]}"


def compute_new_folder(old_folder: str) -> str:
    """La carpeta compartida que corresponde ahora a *old_folder*.

    Solo se renombra si la carpeta se llamaba EXACTAMENTE como la aplicación
    (el caso real: "/datos2/aRenombrar" -> "/datos2/aIBechos"). Si el usuario le
    puso cualquier otro nombre, se respeta y solo cambia el prefijo de los
    archivos de dentro: buscar y reemplazar a ciegas dentro de una ruta escrita
    a mano podría acertar por casualidad en mitad de otro nombre y mandar los
    datos del grupo a una carpeta que no existe."""
    limpia = (old_folder or "").rstrip("/")
    if not limpia:
        return ""
    padre, _, ultimo = limpia.rpartition("/")
    if ultimo != LEGACY_APP_NAME:
        return limpia
    return f"{padre}/{APP_NAME}" if padre else APP_NAME
