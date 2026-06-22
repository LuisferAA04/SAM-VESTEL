"""
Configuración centralizada de logging para SAM
Guarda logs en archivo con rotación automática y muestra en consola
"""

import os
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime

LOG_DIR = os.path.join(os.path.dirname(__file__), 'logs')
if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)

LOG_FILE = os.path.join(LOG_DIR, 'sam.log')

# Formato con timestamp, nivel, módulo y mensaje
FORMATO = '%(asctime)s | %(levelname)-8s | %(name)s | %(message)s'
FORMATO_FECHA = '%Y-%m-%d %H:%M:%S'


def setup_logging(nivel_console=logging.INFO, nivel_archivo=logging.DEBUG):
    """
    Configura logging global.
    - Console: INFO y superior
    - Archivo: DEBUG y superior (más detallado)
    """

    # Logger raíz
    logger_raiz = logging.getLogger()
    logger_raiz.setLevel(logging.DEBUG)

    # Limpiar handlers previos (por si se llama varias veces)
    logger_raiz.handlers = []

    # Handler archivo con rotación (max 5MB, 5 backups)
    handler_archivo = RotatingFileHandler(
        LOG_FILE,
        maxBytes=5 * 1024 * 1024,  # 5 MB
        backupCount=5,
        encoding='utf-8'
    )
    handler_archivo.setLevel(nivel_archivo)
    formato_archivo = logging.Formatter(FORMATO, datefmt=FORMATO_FECHA)
    handler_archivo.setFormatter(formato_archivo)
    logger_raiz.addHandler(handler_archivo)

    # Handler consola (coloreado cuando es posible)
    handler_consola = logging.StreamHandler()
    handler_consola.setLevel(nivel_console)
    formato_consola = logging.Formatter(FORMATO, datefmt=FORMATO_FECHA)
    handler_consola.setFormatter(formato_consola)
    logger_raiz.addHandler(handler_consola)

    return logger_raiz


def get_logger(nombre):
    """Obtener un logger con nombre específico (ej: __name__ en cada módulo)"""
    return logging.getLogger(nombre)


if __name__ == '__main__':
    # Test
    setup_logging()
    log = get_logger(__name__)
    log.debug('Debug message')
    log.info('Info message')
    log.warning('Warning message')
    log.error('Error message')
    print(f"\n✅ Logs guardados en: {LOG_FILE}")
