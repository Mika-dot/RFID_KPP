SELECT TOP 100
    Id,
    RecordTime,
    TagTime,
    Antenna,
    RSSI,
    EPC,
    TID,
    CASE WHEN TID IS NULL OR LTRIM(RTRIM(TID)) = '' THEN 'BAD_EMPTY_TID' ELSE 'OK_TID' END AS TidStatus,
    CONCAT(EPC, ISNULL(TID, '')) AS FullTagCandidate
FROM dbo.RFID_Tags
ORDER BY Id DESC;
