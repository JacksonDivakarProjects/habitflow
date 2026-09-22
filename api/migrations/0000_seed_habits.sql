-- Starter habits (was db/init/02_seed.sql). Existing habits are left alone.
INSERT INTO habits (name, display_name, metric, target_value, target_metric) VALUES
    ('running',            'Running',            'miles',    NULL, NULL),
    ('reading',            'Reading',            'pages',    NULL, NULL),
    ('learning_sql',       'Learning SQL',       'hours',    NULL, NULL),
    ('learning_concepts',  'Learning Concepts',  'concepts', NULL, NULL),
    ('reels',              'Reels',              'hours',    NULL, NULL),
    ('meditation',         'Meditation',         'minutes',  NULL, NULL)
ON CONFLICT (name) DO NOTHING;
