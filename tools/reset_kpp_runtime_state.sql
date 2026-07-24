/*
Сброс runtime-state основного КПП.
Использовать только если таблицы очищали/пересоздавали, а агрегатор перестал видеть новые строки из-за старого LAST_RFID_ID.
После выполнения перезапустить KPP Aggregator.
*/

DELETE FROM dbo.KPP_RuntimeState
WHERE StateKey IN (
    'LAST_RFID_ID',
    'LAST_1C_TASK_ROW_ID',
    'LAST_WAREHOUSE_ROW_ID'
);

SELECT * FROM dbo.KPP_RuntimeState;
