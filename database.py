"""
Base de Datos SAM - Vestel
Gestiona usuarios, conversaciones, casos y estadísticas
"""

import sqlite3
import json
from datetime import datetime
from typing import Optional, Dict, List, Tuple


class Database:
    def __init__(self, db_path="data/sam_database.db"):
        self.db_path = db_path
        self.init_database()
        self._sheets = self._init_sheets()

    def _init_sheets(self):
        """Inicializa la fuente de Google Sheets si está configurada en .env."""
        import os
        if os.getenv('USERS_SOURCE', '').lower() != 'sheets':
            return None
        try:
            from sheets_source import GoogleSheetsUsers
            fuente = GoogleSheetsUsers()
            if fuente.disponible():
                print("✅ Fuente de usuarios: Google Sheets")
                return fuente
            print("⚠️ USERS_SOURCE=sheets pero falta GOOGLE_SHEET_ID — uso SQLite")
        except Exception as e:
            print(f"⚠️ No se pudo iniciar Google Sheets ({e}) — uso SQLite")
        return None
    
    def get_connection(self):
        """Crear conexión a la base de datos"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn
    
    def init_database(self):
        """Inicializar todas las tablas"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        # Tabla de usuarios/clientes (simulada - en producción vendrá de Google Sheets)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS usuarios (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cedula TEXT UNIQUE NOT NULL,
                nombre TEXT NOT NULL,
                telefono TEXT,
                email TEXT,
                plan TEXT,
                codigo_usuario TEXT,
                estado_cuenta TEXT DEFAULT 'activo',
                saldo REAL DEFAULT 0,
                fecha_registro TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Tabla de conversaciones
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS conversaciones (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telefono TEXT NOT NULL,
                cedula TEXT,
                nombre_cliente TEXT,
                estado TEXT DEFAULT 'activo',
                modo TEXT DEFAULT 'automatico',
                ultima_interaccion TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                fecha_inicio TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Tabla de mensajes
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS mensajes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversacion_id INTEGER,
                tipo TEXT NOT NULL,
                contenido TEXT NOT NULL,
                remitente TEXT NOT NULL,
                tokens_usados INTEGER DEFAULT 0,
                fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (conversacion_id) REFERENCES conversaciones (id)
            )
        """)
        
        # Tabla de casos/radicados
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS casos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                radicado TEXT UNIQUE NOT NULL,
                conversacion_id INTEGER,
                cedula TEXT,
                nombre_cliente TEXT,
                telefono TEXT,
                tipo TEXT NOT NULL,
                descripcion TEXT,
                estado TEXT DEFAULT 'abierto',
                prioridad TEXT DEFAULT 'media',
                requiere_escalamiento BOOLEAN DEFAULT 0,
                tipo_escalamiento TEXT,
                datos_json TEXT,
                fecha_creacion TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                fecha_actualizacion TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (conversacion_id) REFERENCES conversaciones (id)
            )
        """)
        
        # Tabla de estadísticas diarias
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS estadisticas_diarias (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fecha DATE UNIQUE NOT NULL,
                total_mensajes INTEGER DEFAULT 0,
                conversaciones_nuevas INTEGER DEFAULT 0,
                casos_creados INTEGER DEFAULT 0,
                escalamientos INTEGER DEFAULT 0
            )
        """)
        
        conn.commit()
        conn.close()
        print("✅ Base de datos inicializada correctamente")
    
    # ═══════════════════════════════════════════════════════════
    # USUARIOS
    # ═══════════════════════════════════════════════════════════
    
    def buscar_usuario(self, cedula: str) -> Optional[Dict]:
        """
        Buscar usuario por cédula.
        Si Google Sheets está configurado como fuente, se consulta allí primero;
        si no se encuentra (o no está configurado), se usa SQLite como respaldo.
        """
        if self._sheets is not None:
            usuario = self._sheets.buscar_usuario(cedula)
            if usuario:
                return usuario
            # No encontrado en Sheets: no caemos a SQLite para no mezclar fuentes
            return None

        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM usuarios WHERE cedula = ?", (cedula,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None
    
    def crear_usuario(self, cedula: str, nombre: str, **kwargs) -> int:
        """Crear usuario (solo para pruebas - en producción viene de Google Sheets)"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        campos = ['cedula', 'nombre']
        valores = [cedula, nombre]
        
        for key, value in kwargs.items():
            if value is not None:
                campos.append(key)
                valores.append(value)
        
        placeholders = ','.join(['?' for _ in valores])
        campos_str = ','.join(campos)
        
        try:
            cursor.execute(
                f"INSERT INTO usuarios ({campos_str}) VALUES ({placeholders})",
                valores
            )
            usuario_id = cursor.lastrowid
            conn.commit()
        except sqlite3.IntegrityError:
            usuario_id = None
        finally:
            conn.close()
        
        return usuario_id
    
    # ═══════════════════════════════════════════════════════════
    # CONVERSACIONES
    # ═══════════════════════════════════════════════════════════
    
    def crear_conversacion(self, telefono: str) -> int:
        """Crear nueva conversación"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO conversaciones (telefono) VALUES (?)",
            (telefono,)
        )
        conversacion_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return conversacion_id
    
    def obtener_conversacion(self, telefono: str) -> Optional[Dict]:
        """Obtener conversación activa por teléfono"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM conversaciones WHERE telefono = ? AND estado = 'activo' ORDER BY id DESC LIMIT 1",
            (telefono,)
        )
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None
    
    def obtener_conversacion_por_id(self, conversacion_id: int) -> Optional[Dict]:
        """Obtener una conversación por su id"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM conversaciones WHERE id = ?", (conversacion_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    def actualizar_conversacion(self, conversacion_id: int, **kwargs):
        """Actualizar conversación"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        campos = []
        valores = []
        
        for key, value in kwargs.items():
            campos.append(f"{key} = ?")
            valores.append(value)
        
        campos.append("ultima_interaccion = CURRENT_TIMESTAMP")
        valores.append(conversacion_id)
        
        cursor.execute(
            f"UPDATE conversaciones SET {', '.join(campos)} WHERE id = ?",
            valores
        )
        conn.commit()
        conn.close()
    
    def obtener_conversaciones_activas(self, limite: int = 50) -> List[Dict]:
        """Obtener lista de conversaciones activas"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT c.*, 
                   (SELECT contenido FROM mensajes WHERE conversacion_id = c.id ORDER BY fecha DESC LIMIT 1) as ultimo_mensaje,
                   (SELECT fecha FROM mensajes WHERE conversacion_id = c.id ORDER BY fecha DESC LIMIT 1) as fecha_ultimo_mensaje
            FROM conversaciones c
            WHERE c.estado = 'activo'
            ORDER BY c.ultima_interaccion DESC
            LIMIT ?
        """, (limite,))
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    
    # ═══════════════════════════════════════════════════════════
    # MENSAJES
    # ═══════════════════════════════════════════════════════════
    
    def guardar_mensaje(self, conversacion_id: int, tipo: str, contenido: str,
                       remitente: str, tokens_usados: int = 0) -> int:
        """Guardar mensaje en la conversación"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO mensajes (conversacion_id, tipo, contenido, remitente, tokens_usados)
            VALUES (?, ?, ?, ?, ?)
        """, (conversacion_id, tipo, contenido, remitente, tokens_usados))
        mensaje_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        # Actualizar estadísticas
        self._actualizar_estadisticas_mensaje()
        
        return mensaje_id
    
    def obtener_mensajes(self, conversacion_id: int, limite: int = 100) -> List[Dict]:
        """Obtener mensajes de una conversación (orden cronológico ascendente, para mostrar)"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM mensajes
            WHERE conversacion_id = ?
            ORDER BY fecha ASC, id ASC
            LIMIT ?
        """, (conversacion_id, limite))
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def obtener_mensajes_recientes(self, conversacion_id: int, limite: int = 20) -> List[Dict]:
        """
        Obtener los ÚLTIMOS N mensajes de una conversación, en orden cronológico.
        Se usa para construir el contexto del modelo: importan los mensajes recientes,
        no los más antiguos.
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM mensajes
            WHERE conversacion_id = ?
            ORDER BY fecha DESC, id DESC
            LIMIT ?
        """, (conversacion_id, limite))
        rows = cursor.fetchall()
        conn.close()
        # Devolvemos en orden ascendente (cronológico) para el historial del chat
        return [dict(row) for row in reversed(rows)]
    
    # ═══════════════════════════════════════════════════════════
    # CASOS/RADICADOS
    # ═══════════════════════════════════════════════════════════
    
    def crear_caso(self, radicado: str, conversacion_id: int, tipo: str,
                   cedula: str = None, nombre_cliente: str = None,
                   telefono: str = None, descripcion: str = None,
                   datos_json: str = None, **kwargs) -> int:
        """Crear caso/radicado"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute("""
                INSERT INTO casos 
                (radicado, conversacion_id, cedula, nombre_cliente, telefono, tipo, descripcion, datos_json, prioridad, requiere_escalamiento, tipo_escalamiento)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                radicado,
                conversacion_id,
                cedula,
                nombre_cliente,
                telefono,
                tipo,
                descripcion,
                datos_json,
                kwargs.get('prioridad', 'media'),
                kwargs.get('requiere_escalamiento', False),
                kwargs.get('tipo_escalamiento')
            ))
            caso_id = cursor.lastrowid
            conn.commit()
        except sqlite3.IntegrityError:
            caso_id = None
        finally:
            conn.close()
        
        return caso_id
    
    def obtener_caso(self, radicado: str) -> Optional[Dict]:
        """Obtener caso por radicado"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM casos WHERE radicado = ?", (radicado,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None
    
    def obtener_casos_recientes(self, limite: int = 20) -> List[Dict]:
        """Obtener casos recientes"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM casos
            ORDER BY fecha_creacion DESC
            LIMIT ?
        """, (limite,))
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    
    def obtener_alertas_activas(self) -> List[Dict]:
        """Obtener casos que requieren escalamiento y están abiertos"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM casos
            WHERE requiere_escalamiento = 1
              AND estado = 'abierto'
            ORDER BY fecha_creacion DESC
        """)
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    
    # ═══════════════════════════════════════════════════════════
    # ESTADÍSTICAS
    # ═══════════════════════════════════════════════════════════
    
    def _actualizar_estadisticas_mensaje(self):
        """Actualizar estadísticas cuando se recibe un mensaje"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        fecha_hoy = datetime.now().date()
        
        cursor.execute("""
            INSERT INTO estadisticas_diarias (fecha, total_mensajes)
            VALUES (?, 1)
            ON CONFLICT(fecha) 
            DO UPDATE SET total_mensajes = total_mensajes + 1
        """, (fecha_hoy,))
        
        conn.commit()
        conn.close()
    
    def obtener_estadisticas(self, dias: int = 7) -> Dict:
        """Obtener estadísticas de los últimos N días"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        # Mensajes hoy (usamos 'localtime' para que coincida con la fecha local guardada)
        cursor.execute("""
            SELECT COALESCE(SUM(total_mensajes), 0) as total
            FROM estadisticas_diarias
            WHERE fecha = DATE('now', 'localtime')
        """)
        mensajes_hoy = cursor.fetchone()['total']

        # Mensajes esta semana
        cursor.execute("""
            SELECT COALESCE(SUM(total_mensajes), 0) as total
            FROM estadisticas_diarias
            WHERE fecha >= DATE('now', 'localtime', '-7 days')
        """)
        mensajes_semana = cursor.fetchone()['total']

        # Mensajes este mes
        cursor.execute("""
            SELECT COALESCE(SUM(total_mensajes), 0) as total
            FROM estadisticas_diarias
            WHERE fecha >= DATE('now', 'localtime', 'start of month')
        """)
        mensajes_mes = cursor.fetchone()['total']

        # Por día (última semana)
        cursor.execute("""
            SELECT fecha, total_mensajes
            FROM estadisticas_diarias
            WHERE fecha >= DATE('now', 'localtime', '-7 days')
            ORDER BY fecha ASC
        """)
        por_dia = [dict(row) for row in cursor.fetchall()]
        
        # Por mes (últimos 6 meses)
        cursor.execute("""
            SELECT strftime('%Y-%m', fecha) as mes, SUM(total_mensajes) as total
            FROM estadisticas_diarias
            WHERE fecha >= DATE('now', '-6 months')
            GROUP BY mes
            ORDER BY mes ASC
        """)
        por_mes = [dict(row) for row in cursor.fetchall()]
        
        conn.close()
        
        return {
            'hoy': mensajes_hoy,
            'semana': mensajes_semana,
            'mes': mensajes_mes,
            'por_dia': por_dia,
            'por_mes': por_mes
        }


# ═══════════════════════════════════════════════════════════
# DATOS DE PRUEBA
# ═══════════════════════════════════════════════════════════

def insertar_datos_prueba():
    """Insertar datos de prueba en la base de datos"""
    db = Database()
    
    # Usuarios de prueba (simulando Google Sheets)
    usuarios_prueba = [
        {
            'cedula': '1234567',
            'nombre': 'Luis Fernando López Pérez',
            'telefono': '+573001234567',
            'plan': '150M + TV',
            'codigo_usuario': '12345',
            'saldo': 0
        },
        {
            'cedula': '7654321',
            'nombre': 'Ana María Martínez García',
            'telefono': '+573009876543',
            'plan': '200M + TV',
            'codigo_usuario': '54321',
            'saldo': 0
        },
        {
            'cedula': '9876543',
            'nombre': 'Carlos Andrés González Silva',
            'telefono': '+573005554321',
            'plan': '100M + TV',
            'codigo_usuario': '98765',
            'saldo': 25000
        }
    ]
    
    for usuario in usuarios_prueba:
        try:
            db.crear_usuario(**usuario)
        except:
            pass  # Ya existe
    
    print("✅ Datos de prueba insertados")


if __name__ == "__main__":
    insertar_datos_prueba()
