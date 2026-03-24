import pyodbc
import logging
import os
import sys
from datetime import datetime

# ==========================================
# ЧТЕНИЕ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ
# ==========================================

def get_env_var(name, required=True):
    """Получает переменную окружения, проверяет наличие"""
    value = os.getenv(name)
    if required and not value:
        logger.error(f"Обязательная переменная окружения '{name}' не найдена!")
        print(f"❌ Ошибка: Переменная окружения '{name}' не установлена.")
        print(f"   Установите её командой: set {name}=значение")
        sys.exit(1)
    return value

# Настройка логирования (до чтения переменных, чтобы видеть ошибки)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('sync_log.txt', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Источник (RusGuardDB)
SRC_CONFIG = {
    'server': get_env_var('SRC_SERVER'),
    'database': get_env_var('SRC_DATABASE'),
    'username': get_env_var('SRC_USERNAME'),
    'password': get_env_var('SRC_PASSWORD'),
    'driver': get_env_var('SRC_DRIVER', required=False) or '{ODBC Driver 17 for SQL Server}'
}

# Приемник (1CTgSend)
DST_CONFIG = {
    'server': get_env_var('DST_SERVER'),
    'database': get_env_var('DST_DATABASE'),
    'username': get_env_var('DST_USERNAME'),
    'password': get_env_var('DST_PASSWORD'),
    'driver': get_env_var('DST_DRIVER', required=False) or '{ODBC Driver 17 for SQL Server}',
    'trust_cert': get_env_var('DST_TRUST_CERT', required=False) or 'yes'
}

# ==========================================
# SQL ЗАПРОСЫ
# ==========================================

QUERY_SELECT = """
SELECT TOP (10000)
  [Log].[_id] AS [ExternalId2],
  [Log].[KeyNumber] AS [PassExternalId2],
  [Log].[EmployeeID] AS [ExternalUserGuid],
  [Log].[DateTime] AS [CreatedAt],
  COALESCE([P1].[Value], [P2].[Value]) AS [PersonControlDeviceName],
  CASE 
    WHEN [LogMsgSubtypes].[Name] LIKE 'Вход%' OR [LogMsgSubtypes].[Name] LIKE 'Въезд%' THEN 'IN'
    WHEN [LogMsgSubtypes].[Name] LIKE 'Выход%' OR [LogMsgSubtypes].[Name] LIKE 'Выезд%' THEN 'OUT'
    ELSE 'UNKNOWN'
  END AS [Direction],
  [LogMsgSubtypes].[Name] AS [ExternalStatus],
  [Log].[LogMessageSubType] AS [ExternalStatusId],
  COALESCE([Employee].[LastName], '') + ' ' + 
  COALESCE([Employee].[FirstName], '') + ' ' + 
  COALESCE([Employee].[SecondName], '') AS [FullName],
  [EmployeeGroup].[Name] AS [FirmName],
  [CardType].[Name] AS [ExternalPassTypeName],
  [AcsKeys].[Name] AS [KeyName],
  COALESCE(
    TRY_PARSE([AcsKeys].[Name] AS INT),
    TRY_PARSE([Employee].[FirstName] AS INT)
  ) AS [CardNumParsed],
  [Log].[KeyNumber] / 65536 AS [CardNumReal]
FROM [Log]
  INNER JOIN [LogMsgSubtypes] ON [Log].[LogMessageSubType] = [LogMsgSubtypes].[Id]
  INNER JOIN [Employee] ON [Log].[EmployeeID] = [Employee].[_id]
  INNER JOIN [EmployeeGroup] ON [Employee].[EmployeeGroupID] = [EmployeeGroup].[_id]
  INNER JOIN [AcsKeys] ON [Log].[KeyNumber] = [AcsKeys].[KeyNumber]
  INNER JOIN [AcsKey2EmployeeAssignment] ON [AcsKeys].[KeyNumber] = [AcsKey2EmployeeAssignment].[AcsKeyId]
  LEFT JOIN [CardType] ON [AcsKeys].[CardTypeID] = [CardType].[_id]
  LEFT JOIN [Property] [P1] ON [Log].[DriverID] = [P1].[_idResource] AND [P1].[PropertyName] = 'HardwareName'
  LEFT JOIN [Property] [P2] ON [Log].[DriverID] = [P2].[_idResource] AND [P2].[PropertyName] = 'Name'
WHERE
  [LogMsgSubtypes].[Name] IN (
    'Вход', 'Вход с подтверждением', 'Вход по ключу', 'Въезд', 'Въезд по ключу', 'Въезд с подтверждением', 'Вход по лицу', 'Въезд по лицу',
    'Выход', 'Выход по считывателю картоприёмника', 'Выход с подтверждением', 'Выход по считывателю картоприёмника с подтверждением', 
    'Выход по ключу', 'Выезд', 'Выезд по считывателю картоприёмника', 'Выезд по ключу', 'Выезд с подтверждением', 
    'Выезд по считывателю картоприёмника с подтверждением', 'Выход по лицу', 'Выезд по лицу'
  )
  AND [Log].[DateTime] >= (GETDATE() - 1)
  AND LOWER(LTRIM(RTRIM(COALESCE([P1].[Value], [P2].[Value])))) = 'ворота змк юг'
  AND [Employee].[IsRemoved] = 0
  AND [Employee].[IsLocked] = 0
  AND [EmployeeGroup].[IsRemoved] = 0
  AND [AcsKey2EmployeeAssignment].[AssignmentModificationType] = 0
ORDER BY [Log].[DateTime] ASC
"""

QUERY_CHECK_EXISTS = """
SELECT ExternalId2 FROM [dbo].[RusGuardLogs] WHERE ExternalId2 = ?
"""

QUERY_INSERT = """
INSERT INTO [dbo].[RusGuardLogs] (
    [ExternalId2], [PassExternalId2], [ExternalUserGuid], [CreatedAt], 
    [PersonControlDeviceName], [Direction], [ExternalStatus], [ExternalStatusId], 
    [FullName], [FirmName], [ExternalPassTypeName], [KeyName], 
    [CardNumParsed], [CardNumReal]
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

# ==========================================
# ФУНКЦИИ
# ==========================================

def get_connection(config):
    """Создает и возвращает подключение к БД"""
    conn_str = (
        f"DRIVER={config['driver']};"
        f"SERVER={config['server']};"
        f"DATABASE={config['database']};"
        f"UID={config['username']};"
        f"PWD={config['password']};"
    )
    if config.get('trust_cert'):
        conn_str += "TrustServerCertificate=yes;"
        
    try:
        conn = pyodbc.connect(conn_str, autocommit=False)
        return conn
    except pyodbc.Error as e:
        logger.error(f"Ошибка подключения к {config['database']}: {e}")
        raise

def sync_data():
    """Основная функция синхронизации"""
    logger.info("=== Начало синхронизации ===")
    
    src_conn = None
    dst_conn = None
    cursor_src = None
    cursor_dst = None
    
    try:
        src_conn = get_connection(SRC_CONFIG)
        cursor_src = src_conn.cursor()
        logger.info("Подключение к источнику (RusGuardDB) успешно")
        
        cursor_src.execute(QUERY_SELECT)
        rows = cursor_src.fetchall()
        total_rows = len(rows)
        logger.info(f"Получено записей из источника: {total_rows}")
        
        if total_rows == 0:
            logger.info("Новых данных для переноса нет.")
            return

        dst_conn = get_connection(DST_CONFIG)
        cursor_dst = dst_conn.cursor()
        logger.info("Подключение к приемнику (1CTgSend) успешно")
        
        inserted_count = 0
        skipped_count = 0
        
        for row in rows:
            external_id = row.ExternalId2
            
            cursor_dst.execute(QUERY_CHECK_EXISTS, (external_id,))
            if cursor_dst.fetchone():
                skipped_count += 1
                continue
            
            values = (
                row.ExternalId2,
                row.PassExternalId2,
                str(row.ExternalUserGuid) if row.ExternalUserGuid else None,
                row.CreatedAt,
                row.PersonControlDeviceName,
                row.Direction,
                row.ExternalStatus,
                row.ExternalStatusId,
                row.FullName,
                row.FirmName,
                row.ExternalPassTypeName,
                row.KeyName,
                row.CardNumParsed,
                row.CardNumReal
            )
            
            try:
                cursor_dst.execute(QUERY_INSERT, values)
                inserted_count += 1
            except Exception as e:
                logger.error(f"Ошибка вставки записи {external_id}: {e}")
        
        dst_conn.commit()
        logger.info(f"Транзакция зафиксирована. Вставлено: {inserted_count}, Пропущено (дубли): {skipped_count}")
        
    except Exception as e:
        logger.error(f"Критическая ошибка синхронизации: {e}")
        if dst_conn:
            dst_conn.rollback()
            logger.warning("Выполнен откат транзакции")
    finally:
        if cursor_src: cursor_src.close()
        if src_conn: src_conn.close()
        if cursor_dst: cursor_dst.close()
        if dst_conn: dst_conn.close()
        logger.info("=== Синхронизация завершена ===\n")

if __name__ == "__main__":
    # Вывод информации о загруженных переменных (без паролей!)
    logger.info("Загруженные конфигурации:")
    logger.info(f"  Источник: {SRC_CONFIG['server']} / {SRC_CONFIG['database']}")
    logger.info(f"  Приемник: {DST_CONFIG['server']} / {DST_CONFIG['database']}")
    
    import time
    try:
        while True:
            sync_data()
            time.sleep(60)  # Пауза 60 секунд
    except KeyboardInterrupt:
        logger.info("Остановка скрипта пользователем")