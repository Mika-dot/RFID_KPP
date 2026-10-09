import importlib.util
import json
import sys
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from common.bus_statistics import RuntimeStatistics
from observer.catalog import coverage, PLANNED_METRICS
from observer.collector import sql_metrics
from observer.detailed import summarize, ambiguous_series_count


class Cursor:
    def __init__(self, rows=(), names=()):
        self.rows=list(rows);self.description=[(name,) for name in names]
    def fetchone(self):return self.rows[0] if self.rows else None
    def fetchall(self):return self.rows


class ReadOnlyFixture:
    def __init__(self, data):
        self.data=data;self.queries=[];self.closed=False
    def execute(self, query, *params):
        self.queries.append(query)
        if query.startswith("SET "):return Cursor()
        if "DATEADD(minute,-5" in query:return Cursor([(NOW-timedelta(minutes=5),NOW)])
        if "MAX(Id)" in query:return Cursor([(20,)])
        if "TRY_CONVERT" in query:return Cursor([(18,)])
        source=next((key for key in self.data if "FROM "+key+" " in query),None)
        if source is None:raise RuntimeError("UnexpectedTable")
        rows=self.data[source]
        if "COUNT_BIG" in query:return Cursor([(len(rows),)])
        if "SELECT TOP" in query:
            if "Tag,SeriesNumber,Dt" in query:
                return Cursor([(r.get("Tag"),r.get("SeriesNumber"),r.get("Dt")) for r in rows])
            names=list(rows[0]) if rows else []
            return Cursor([tuple(r[n] for n in names) for r in rows],names)
        if "SELECT Id,Tag,Ids,SeriesNumber" in query:
            return Cursor([(r["Id"],r.get("Tag"),r.get("Ids"),r.get("SeriesNumber")) for r in rows if r["Id"] in params])
        if "SELECT DISTINCT" in query:return Cursor()
        raise RuntimeError("UnexpectedQuery")
    def close(self):self.closed=True


NOW=datetime(2026,10,9,9,0)


class DetailedMetricsTests(unittest.TestCase):
    def test_series_ambiguity_uses_distinct_tags_in_each_inclusive_window(self):
        tasks=[("TagA","s",NOW-timedelta(hours=1)),("taga","S",NOW),
               ("TagB","S",NOW+timedelta(hours=1)),("TagC","other",NOW),
               ("TagA","s",NOW+timedelta(hours=6))]
        rows=[dict(SeriesNumber="s",Dt=NOW),dict(SeriesNumber=" S ",Dt=NOW+timedelta(hours=6))]
        self.assertEqual(1,ambiguous_series_count(rows,tasks,1))

    def test_source_order_ignores_negative_time_gap_and_outputs_no_identifiers(self):
        rows=[dict(Id=2,EPC="private-epc",TID="private-tid",Antenna=2,RecordTime=NOW,RSSI=-40,ReceivedAt=NOW+timedelta(seconds=2)),
              dict(Id=1,EPC="private-epc",TID="private-tid",Antenna=1,RecordTime=NOW+timedelta(seconds=1),RSSI=-60,TimeQuality="APPROXIMATE",ReceivedAt=NOW+timedelta(seconds=2))]
        values=summarize("rfid",rows)
        self.assertEqual(1,values["rfid_antenna_transition_matrix_1_2"])
        self.assertNotIn("rfid_inter_read_gap",values)
        self.assertEqual(-50,values["rfid_rssi_median"])
        self.assertEqual(.5,values["rfid_approximate_time_ratio"])
        self.assertNotIn("private",json.dumps(values))

    def test_processing_latency_uses_original_close_not_recheck_timestamp(self):
        values=summarize("events",[dict(RfidReadCount=1,IsReel=1,PassageGroupKey=None,
            FirstSeen=NOW-timedelta(seconds=4),LastSeen=NOW,CompletedAt=NOW+timedelta(seconds=3),UpdatedAt=NOW+timedelta(hours=24))])
        self.assertEqual(3,values["processing_latency"])
        self.assertNotIn("passage_group_reel_count",values)
        self.assertEqual(4,values["rfid_session_duration"])
        self.assertEqual({},summarize("rfid",[]))

    def test_runtime_window_is_bounded_and_truncated_populations_are_unknown(self):
        now=[0];stats=RuntimeStatistics(limit=2,window=10,clock=lambda:now[0])
        stats.add("camera_0_fresh_fps");stats.add("camera_0_stale_rate");now[0]=2
        self.assertEqual(.5,stats.snapshot()["camera_0_stale_ratio"])
        stats.add("camera_0_fresh_fps");stats.add("camera_0_fresh_fps")
        self.assertNotIn("camera_0_fresh_fps",stats.snapshot())
        self.assertNotIn("camera_0_stale_ratio",stats.snapshot())
        stats.add("latency",float("nan"));self.assertNotIn("latency",stats.snapshot())
        now[0]=15
        self.assertEqual(0,stats.snapshot()["camera_0_fresh_fps"])
        self.assertNotIn("camera_0_stale_ratio",stats.snapshot())

    def test_catalog_has_42_instruments_and_does_not_invent_physical_rto(self):
        planned={name for names in PLANNED_METRICS.values() for name in names}
        rows=[r for r in coverage({"rfid_antenna_transition_matrix_1_2":7}) if r["metric"] in planned]
        self.assertEqual(43,len(rows))
        self.assertEqual(["ha_physical_business_rto"],[r["metric"] for r in rows if r["state"]=="not_instrumented"])
        self.assertEqual("observed",next(r["state"] for r in rows if r["metric"]=="rfid_antenna_transition_matrix"))

    def fixtures(self):
        events=ReadOnlyFixture({"dbo.RFID_Tags":[dict(Id=1,EPC="AB",TID="C",RSSI=-50,Antenna=1,RecordTime=NOW),
            dict(Id=2,EPC="A",TID="BC",RSSI=float("nan"),Antenna=2,RecordTime=NOW)],
            "dbo.ReelTransitions":[],"dbo.RusGuardLogs":[],
            "dbo.KPP_ReelEvents":[dict(EventId=1,RfidReadCount=2,IsReel=1,PassageGroupKey="group",WarehouseId=5,SourceTag="TAG",Task1CId=6)],
            "dbo.KPP_EventVideoLinks":[]})
        tasks=ReadOnlyFixture({"dbo.Warehouse":[dict(Id=5,Tag="TAG",Ids=None,SeriesNumber="s",Dt=NOW)],
            "dbo.RfidTags":[dict(Id=6,Tag="TAG",Ids=None,SeriesNumber="s",Dt=NOW)]})
        return events,tasks

    @patch.dict("os.environ",{},clear=True)
    def test_independent_task_database_counts_and_links_without_cross_db_join(self):
        events,tasks=self.fixtures();values=sql_metrics(events,tasks)
        self.assertEqual(1,values["warehouse_tag_links_5min"])
        self.assertEqual(2,values["rfid_unique_epc_tid_5min"])
        self.assertEqual(-50,values["rfid_avg_rssi"])
        self.assertEqual(2,values["cursor_lag"])
        self.assertTrue(events.closed and tasks.closed)
        self.assertFalse(values["_unavailable_sources"])
        self.assertFalse(any("FROM dbo.Warehouse " in q or "FROM dbo.RfidTags " in q for q in events.queries))
        for query in events.queries+tasks.queries:
            self.assertTrue(query.startswith(("SELECT ","SET LOCK_TIMEOUT")))
            self.assertNotIn(" JOIN ",query);self.assertNotIn("CardNum",query);self.assertNotIn("FullName",query)

    @patch.dict("os.environ",{},clear=True)
    def test_truncation_keeps_exact_counts_but_omits_subset_statistics(self):
        events,tasks=self.fixtures();values=sql_metrics(events,tasks,row_limit=1)
        self.assertEqual(2,values["rfid_reads_5min"])
        self.assertNotIn("rfid_avg_rssi",values)
        self.assertIn("rfid:population_truncated",values["_unavailable_sources"])

    @patch.dict("os.environ",{"PERIMETER_OBSERVER_TASK_SQL":"private-task-connection"},clear=True)
    def test_optional_task_connection_failure_preserves_other_buses(self):
        events,_=self.fixtures()
        with patch.dict(sys.modules,{"pyodbc":SimpleNamespace(connect=Mock(side_effect=TimeoutError()))}):
            values=sql_metrics(events)
        self.assertEqual(2,values["rfid_reads_5min"])
        self.assertNotIn("warehouse_rows_5min",values)
        self.assertNotIn("warehouse_tag_links_5min",values)
        self.assertIn("task_connection:TimeoutError",values["_unavailable_sources"])
        self.assertTrue(events.closed)

    @unittest.skipIf(importlib.util.find_spec("cv2") is None,"OpenCV unavailable")
    def test_production_video_adapter_counts_new_tracks_before_hit_threshold(self):
        from deploy import monitored_yolo as adapter
        app=adapter._load_app();reporter=Mock();reporter.required_dependencies=()
        detection=app.Detection(app.Config.REEL_CLASS,.9,(1,1,10,10))
        app.detect=Mock(return_value=("frame",[detection]))
        def exercise():
            tracker=app.CentroidTracker(0)
            for _ in range(3):tracker.update([detection],NOW,(100,100))
            self.assertEqual(("frame",[detection]),app.detect())
            return 0
        app.main=exercise
        with patch.object(adapter,"_load_app",return_value=app),patch.object(adapter,"get_reporter",return_value=reporter),patch.object(adapter,"record") as record:
            self.assertEqual(0,adapter.main())
        names=[call.args[0] for call in record.call_args_list]
        self.assertEqual(1,names.count("video_tracks_per_min"))
        self.assertEqual(1,names.count("video_detection_rate"))
