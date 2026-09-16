SELECT event_time, channel, region, product_id,
       SUM(revenue) AS revenue, COUNT(*) AS events,
       1.0 * SUM(converted) / NULLIF(COUNT(*), 0) AS conversion_rate
FROM mock_events
WHERE ((event_time >= '2026-08-01 00:00:00' AND event_time < '2026-08-08 00:00:00') OR (event_time >= '2026-08-15 00:00:00' AND event_time < '2026-08-22 00:00:00'))
GROUP BY event_time, channel, region, product_id
ORDER BY event_time, channel, region, product_id;
