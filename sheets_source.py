"""
Fuente de usuarios/clientes desde Google Sheets (modo link público / CSV).

La hoja debe estar compartida como "Cualquiera con el enlace → Lector".
Se lee vía la URL de exportación CSV de Google, sin credenciales.

Configuración por .env:
    USERS_SOURCE=sheets
    GOOGLE_SHEET_ID=<id de la hoja>      # el tramo largo entre /d/ y /edit
    GOOGLE_SHEET_CACHE_TTL=60            # segundos de caché (opcional)

    # Opción A — una sola pestaña (con o sin columna 'estado'):
    GOOGLE_SHEET_GID=0

    # Opción B — varias pestañas, el estado se toma del nombre de la pestaña.
    # Formato: nombre:gid separados por coma. Si está definida, tiene prioridad.
    GOOGLE_SHEET_TABS=compromiso:0,exonerado:123456,cortados:789012
"""

import os
import csv
import io
import time
import re
import unicodedata
import requests


def _normalizar(texto):
    """Minúsculas, sin acentos y sin espacios extra — para comparar encabezados."""
    if texto is None:
        return ""
    texto = unicodedata.normalize('NFKD', str(texto))
    texto = texto.encode('ascii', 'ignore').decode('ascii')
    return texto.strip().lower()


def _solo_digitos(texto):
    return re.sub(r'\D', '', str(texto or ''))


# Sinónimos aceptados para cada campo estándar (encabezados normalizados).
# Ajusta/añade aquí según los nombres reales de las columnas de tu hoja.
MAPEO_COLUMNAS = {
    'cedula':         ['cedula', 'documento', 'cc', 'nro documento', 'numero documento', 'identificacion'],
    'nombre':         ['nombre', 'nombres', 'nombre completo', 'cliente', 'titular'],
    'telefono':       ['telefono', 'celular', 'movil', 'tel', 'whatsapp', 'numero'],
    'email':          ['email', 'correo', 'correo electronico', 'e-mail'],
    'plan':           ['plan', 'plan actual', 'servicio', 'paquete',
                       'serv. suscritos', 'serv suscritos', 'servicios suscritos',
                       'servicios', 'servicio suscrito'],
    'codigo_usuario': ['codigo_usuario', 'codigo usuario', 'codigo', 'usuario', 'id usuario', 'id cliente', 'codigo cliente'],
    'estado_cuenta':  ['estado_cuenta', 'estado', 'estado cuenta', 'estado de cuenta'],
    'saldo':          ['saldo', 'mora', 'deuda', 'saldo pendiente', 'valor adeudado'],
}


class GoogleSheetsUsers:
    def __init__(self, sheet_id=None, gid=None, cache_ttl=None):
        self.sheet_id = sheet_id or os.getenv('GOOGLE_SHEET_ID', '')
        self.gid = gid or os.getenv('GOOGLE_SHEET_GID', '0')
        self.cache_ttl = int(cache_ttl or os.getenv('GOOGLE_SHEET_CACHE_TTL', '60'))
        # Pestañas múltiples: "nombre:gid,nombre:gid" -> [(estado, gid), ...]
        self.tabs = self._parsear_tabs(os.getenv('GOOGLE_SHEET_TABS', ''))
        self._cache_filas = None
        self._cache_ts = 0

    @staticmethod
    def _parsear_tabs(valor):
        tabs = []
        for parte in valor.split(','):
            parte = parte.strip()
            if not parte or ':' not in parte:
                continue
            nombre, gid = parte.rsplit(':', 1)
            tabs.append((nombre.strip(), gid.strip()))
        return tabs

    # ── descarga / parseo ──────────────────────────────────────────────

    def _csv_url(self, gid):
        return (
            f"https://docs.google.com/spreadsheets/d/{self.sheet_id}"
            f"/export?format=csv&gid={gid}"
        )

    @property
    def csv_url(self):
        return self._csv_url(self.gid)

    def _descargar_gid(self, gid, estado=None):
        """Descarga y parsea una pestaña. Si se da 'estado', lo asigna a todas sus filas."""
        resp = requests.get(self._csv_url(gid), timeout=10)
        resp.raise_for_status()
        resp.encoding = 'utf-8'
        filas = self._parsear_csv(resp.text)
        if estado:
            for f in filas:
                f['estado_cuenta'] = estado
        return filas

    def _descargar(self):
        """Devuelve la lista de filas (dicts con campos estándar). Usa caché TTL."""
        ahora = time.time()
        if self._cache_filas is not None and (ahora - self._cache_ts) < self.cache_ttl:
            return self._cache_filas

        if not self.sheet_id:
            print("⚠️ GOOGLE_SHEET_ID no configurado")
            return []

        try:
            if self.tabs:
                # Varias pestañas: el estado se toma del nombre de cada una
                filas = []
                for nombre, gid in self.tabs:
                    filas.extend(self._descargar_gid(gid, estado=nombre))
            else:
                filas = self._descargar_gid(self.gid)
            self._cache_filas = filas
            self._cache_ts = ahora
            return filas
        except Exception as e:
            print(f"❌ Error leyendo Google Sheets: {e}")
            # Si hay caché vieja, mejor devolverla que nada
            return self._cache_filas or []

    def _parsear_csv(self, texto):
        lector = csv.reader(io.StringIO(texto))
        filas = list(lector)
        if not filas:
            return []

        encabezados = [_normalizar(h) for h in filas[0]]

        # Resolver el índice de columna para cada campo estándar
        indices = {}
        for campo, sinonimos in MAPEO_COLUMNAS.items():
            for i, h in enumerate(encabezados):
                if h in sinonimos:
                    indices[campo] = i
                    break

        resultado = []
        for fila in filas[1:]:
            if not any(c.strip() for c in fila):
                continue  # fila vacía
            registro = {}
            for campo, idx in indices.items():
                valor = fila[idx].strip() if idx < len(fila) else ''
                registro[campo] = valor
            # Normalizaciones de tipo
            registro['cedula'] = _solo_digitos(registro.get('cedula'))
            registro['saldo'] = self._a_numero(registro.get('saldo'))
            if not registro.get('estado_cuenta'):
                registro['estado_cuenta'] = 'activo'
            resultado.append(registro)
        return resultado

    @staticmethod
    def _a_numero(valor):
        digitos = re.sub(r'[^\d]', '', str(valor or ''))
        return float(digitos) if digitos else 0.0

    # ── API pública ────────────────────────────────────────────────────

    def buscar_usuario(self, cedula):
        """Devuelve el dict del usuario cuyo documento coincide, o None."""
        objetivo = _solo_digitos(cedula)
        if not objetivo:
            return None
        for registro in self._descargar():
            if registro.get('cedula') == objetivo:
                return registro
        return None

    def disponible(self):
        """True si hay un sheet_id configurado."""
        return bool(self.sheet_id)


if __name__ == '__main__':
    from dotenv import load_dotenv
    load_dotenv()
    fuente = GoogleSheetsUsers()
    print("URL CSV:", fuente.csv_url)
    filas = fuente._descargar()
    print(f"Filas leídas: {len(filas)}")
    if filas:
        print("Ejemplo:", filas[0])
