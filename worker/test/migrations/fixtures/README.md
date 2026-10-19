# Frozen rename artifacts

`rename.sql` and `rename-snapshot.json` are actual Drizzle Kit 0.31.11 output for
renaming the post-0003 `artists.image_file_id` column to `portrait_file_id`, using
ORM0.45.4. The author selected the rename in Kit's interactive prompt. Tests pair
these with the unchanged post-0003 baseline and a synthetic idx4 journal entry;
CI never guesses a rename or waits on a prompt. These are test artifacts, not
production migration files. Other add/drop/rebuild fixtures are generated during
the test run in disposable directories.
