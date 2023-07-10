import bz2
import os
import sqlite3
import time
import re
from log_to_txt import save_log

DB = None
DBPATH = r"db.sqlite"
SCHEMA_VERSION = 9
SCHEMA = r"""
CREATE TABLE IF NOT EXISTS test_suites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT,
    spec TEXT
);
CREATE TABLE IF NOT EXISTS machine_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT
);
CREATE TABLE IF NOT EXISTS machines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_group_id INTEGER REFERENCES machine_groups(id) ON DELETE CASCADE,
    hostname TEXT UNIQUE,
    description TEXT,
    valid BOOLEAN
);
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT,
    path_pattern TEXT
);
CREATE TABLE IF NOT EXISTS autorun_items (
    -- This field is not strictly speaking needed, but the API
    -- does DB syncing via row ids, so...
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER REFERENCES products(id) ON DELETE CASCADE,
    test_suite_id INTEGER REFERENCES test_suites(id) ON DELETE CASCADE,
    machine_group_id INTEGER REFERENCES machine_groups(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS builds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- XXX: Should it be CASCADE or SET NULL?
    product_id INTEGER REFERENCES products(id) ON DELETE CASCADE,
    name TEXT,
    path TEXT,
    date INTEGER -- Dates are stored as Unix timestamps (in UTC)
);
CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    build_id INTEGER REFERENCES builds(id) ON DELETE CASCADE,
    test_suite_id INTEGER REFERENCES test_suites(id) ON DELETE CASCADE,
    machine_group_id INTEGER REFERENCES machine_groups(id) ON DELETE CASCADE,
    log_path TEXT,
    start_date INTEGER,
    end_date INTEGER
);
CREATE TABLE IF NOT EXISTS test_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    result_id INTEGER REFERENCES results(id) ON DELETE CASCADE,
    machine_id INTEGER REFERENCES machines(id) ON DELETE CASCADE,
    log_path TEXT,
    start_date INTEGER,
    end_date INTEGER
);
-- TODO: try storing unique test names in a separate table for compression
CREATE TABLE IF NOT EXISTS result_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    result_id INTEGER REFERENCES results(id) ON DELETE CASCADE,
    test_name TEXT,
    test_passed INTEGER
);
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT
);
CREATE TABLE IF NOT EXISTS contact_assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id INTEGER REFERENCES contacts(id) ON DELETE CASCADE,
    test_suite_id INTEGER REFERENCES test_suites(id) ON DELETE CASCADE,
    test_name_pattern TEXT
);
CREATE TABLE IF NOT EXISTS contact_telegram (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id TEXT,
    contact_name TEXT
);
-- The table store the build to send a message about its results
CREATE TABLE IF NOT EXISTS builds_to_send_result (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- XXX: Should it be CASCADE or SET NULL?
    product_id INTEGER REFERENCES products(id) ON DELETE CASCADE,
    name TEXT,
    path TEXT,
    date INTEGER -- Dates are stored as Unix timestamps (in UTC)
);

-- There are many result_items, and they are always selected by
-- result_id.
CREATE INDEX IF NOT EXISTS i_result_items_by_result_id
    ON result_items(result_id);

-- The latest build from a given path is often needed.
CREATE INDEX IF NOT EXISTS i_builds_by_path_and_date
    ON builds(path, date);
"""

MIN_SUPPORTED_SCHEMA_VERSION = 1
MIGRATION = {
    (1, 2): r"""
ALTER TABLE results ADD COLUMN log_path TEXT;
""",
    (2, 3): r"""
PRAGMA foreign_keys = OFF;
CREATE TABLE IF NOT EXISTS machines_v3 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_group_id INTEGER REFERENCES machine_groups(id) ON DELETE CASCADE,
    hostname TEXT UNIQUE,
    valid BOOLEAN
);
INSERT INTO machines_v3 (id, machine_group_id, hostname, valid)
    SELECT id, machine_group_id, hostname, valid FROM machines
        WHERE machine_group_id IS NOT NULL;
DROP TABLE machines;
ALTER TABLE machines_v3 RENAME TO machines;
PRAGMA foreign_keys = ON;
""",
    (3, 4): r"""
PRAGMA foreign_keys = OFF;
CREATE TABLE IF NOT EXISTS test_suites_v4 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT,
    spec TEXT
);
INSERT INTO test_suites_v4 (id, title, spec)
    SELECT
        id,
        title,
        '{"kind":"TC",' ||
        '"path":"' || replace(path, '\', '\\') || '",' ||
        '"routine":"' || routine || '"}'
    FROM test_suites;
DROP TABLE test_suites;
ALTER TABLE test_suites_v4 RENAME TO test_suites;
PRAGMA foreign_keys = ON;
""",
    (4, 5): r"""
ALTER TABLE result_sets RENAME TO autorun_items;
""",
    (5, 6): r"""
PRAGMA foreign_keys = OFF;
CREATE TABLE IF NOT EXISTS result_items_v6 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    result_id INTEGER REFERENCES results(id) ON DELETE CASCADE,
    test_name TEXT,
    test_passed INTEGER
);
INSERT
    INTO result_items_v6 (id, result_id, test_name, test_passed)
    SELECT id, result_id, test_name, test_result==0 FROM result_items;
DROP TABLE result_items;
ALTER TABLE result_items_v6 RENAME TO result_items;
PRAGMA foreign_keys = ON;
""",
    (6, 7): r"""
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT
);
CREATE TABLE IF NOT EXISTS contact_assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id INTEGER REFERENCES contacts(id) ON DELETE CASCADE,
    test_suite_id INTEGER REFERENCES test_suites(id) ON DELETE CASCADE,
    test_name_pattern TEXT
);
""",
    (7, 8): r"""
UPDATE test_suites SET spec=replace(spec, '"branch"', '"revision"');
""",
    (8, 9): r"""
ALTER TABLE machines ADD COLUMN description TEXT;
""",
}

def init():
    global DB
    DB = sqlite3.connect(DBPATH, check_same_thread=False)
    DB.row_factory = sqlite3.Row
    DB.execute("PRAGMA foreign_keys = ON")
    DB.execute("PRAGMA temp_store = MEMORY")
    version = DB.execute("PRAGMA user_version").fetchone()["user_version"]
    if version == 0 or version == SCHEMA_VERSION:
        with DB:
            DB.executescript(SCHEMA)
            DB.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)
    elif MIN_SUPPORTED_SCHEMA_VERSION <= version < SCHEMA_VERSION:
        backup("before-upgrade-from-v%d" % version)
        with DB:
            for i in range(version, SCHEMA_VERSION):
                DB.executescript(MIGRATION[(i, i + 1)])
            DB.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)
    else:
        raise Exception("database version '%s' is not supported" % version)

def backup(tag=None):
    filename = "backup" if tag is None else "backup." + tag
    filename += time.strftime(".%Y-%m-%d.%H-%M.sqlite.bz2")
    filename_tmp = filename + ".tmp"
    try:
        start_time = time.time()
        print("backing up the db into", filename)
        DB.execute("BEGIN EXCLUSIVE")
        try:
            with bz2.open(filename_tmp, "wb") as fout:
                with open(DBPATH, "rb") as fin:
                    while True:
                        chunk = fin.read1(1024*1024)
                        if not chunk: break
                        fout.write(chunk)
        finally:
            DB.rollback()
        end_time = time.time()
        os.rename(filename_tmp, filename)
        print("... backup succeeded in", end_time - start_time, "seconds")
    except Exception as e:
        print("... backup failed")
        os.remove(filename_tmp)
        raise e

## TEST_SUITES

def create_test_suite(title, spec):
    c = DB.execute("INSERT INTO test_suites (title, spec) "
                   "VALUES (?, ?)", (title, spec))
    return c.lastrowid

def iter_test_suites():
    return DB.execute("SELECT * FROM test_suites")

def get_test_suite(rowid):
    c = DB.execute("SELECT * FROM test_suites WHERE id=?", (rowid,))
    return c.fetchone()

def update_test_suite(rowid, title, spec):
    DB.execute("UPDATE test_suites "
                   "SET title=?, spec=? WHERE id=?",
               (title, spec, rowid))

def delete_test_suite(rowid):
    DB.execute("DELETE FROM test_suites WHERE id=?", (rowid,))

## MACHINE_GROUPS

def create_machine_group(title):
    c = DB.execute("INSERT INTO machine_groups (title) "
                   "VALUES (?)", (title, ))
    return c.lastrowid

def get_machine_group(rowid):
    c = DB.execute("SELECT * FROM machine_groups WHERE id=?", (rowid,))
    return c.fetchone()

def iter_machine_groups():
    return DB.execute("SELECT * FROM machine_groups")

def update_machine_group(rowid, title):
    DB.execute("UPDATE machine_groups SET title=? WHERE id=?",
               (title, rowid))

def delete_machine_group(rowid):
    DB.execute("DELETE FROM machine_groups WHERE id=?", (rowid,))

## MACHINES

def create_machine(machine_group_id, hostname, description):
    c = DB.execute(
        "INSERT INTO machines (machine_group_id, hostname, description) "
        "VALUES (?, ?, ?)", (machine_group_id, hostname, description))
    return c.lastrowid

def iter_machines():
    return DB.execute("SELECT * FROM machines")

def get_machine(rowid):
    c = DB.execute("SELECT * FROM machines WHERE id=?", (rowid,))
    return c.fetchone()

def get_machine_by_hostname(hostname):
    c = DB.execute("SELECT * FROM machines WHERE hostname=?", (hostname,))
    return c.fetchone()

def update_machine(rowid, machine_group_id, hostname, description, valid):
    DB.execute(
        "UPDATE machines "
            "SET machine_group_id=?, hostname=?, description=?, valid=? "
            "WHERE id=?",
        (machine_group_id, hostname, description, valid, rowid))

def update_machine_valid(rowid, valid):
    DB.execute("UPDATE machines SET valid=? WHERE id=?", (valid, rowid))

def delete_machine(rowid):
    DB.execute("DELETE FROM machines WHERE id=?", (rowid,))

## PRODUCTS

def create_product(title, path_pattern):
    c = DB.execute("INSERT INTO products (title, path_pattern) "
                   "VALUES (?, ?)", (title, path_pattern))
    return c.lastrowid

def get_product(rowid):
    c = DB.execute("SELECT * FROM products WHERE id=?", (rowid,))
    return c.fetchone()

def iter_products():
    return DB.execute("SELECT * FROM products")

def update_product(rowid, title, path_pattern):
    DB.execute("UPDATE products SET title=?, path_pattern=? "
               "WHERE id=?", (title, path_pattern, rowid))

def delete_product(rowid):
    DB.execute("DELETE FROM products WHERE id=?", (rowid,))

## RESULT SETS

# XXX: create_or_select
def create_autorun_item(product_id, test_suite_id, machine_group_id):
    c = DB.execute("INSERT INTO autorun_items "
                       "(product_id, test_suite_id, machine_group_id) "
                   "VALUES (?, ?, ?)",
                   (product_id, test_suite_id, machine_group_id))
    return c.lastrowid

def iter_autorun_items():
    return DB.execute("SELECT * FROM autorun_items")

def iter_autorun_items_by_product(product_id):
    return DB.execute("SELECT * FROM autorun_items WHERE product_id=?",
                      (product_id,))

def delete_autorun_item(rowid):
    DB.execute("DELETE FROM autorun_items WHERE id=?", (rowid,))

## BUILDS

def create_build(product_id, name, path, date):
    c = DB.execute("INSERT INTO builds (product_id, name, path, date) "
                   "VALUES (?, ?, ?, ?)", (product_id, name, path, date))
    return c.lastrowid

def create_build_to_send_result(product_id, name, path, date):
    if(name.find("URSApplicationStudio-11")!=-1):
        c = DB.execute("INSERT INTO builds_to_send_result (product_id, name, path, date) "
                       "VALUES (?, ?, ?, ?)", (product_id, name, path, date))
        return c.lastrowid
    else:
        pass

def get_build_id_to_send_result(product_id):
    c = DB.execute(
            "SELECT product_id FROM builds_to_send_result WHERE product_id "
            "ORDER BY date ASC LIMIT 1", )
    return c.fetchone()

def get_name_to_send_result():
    c = DB.execute(
            "SELECT name FROM builds_to_send_result  "
            "ORDER BY date ASC LIMIT 1", )
    return c.fetchone()

def get_build_name_by_id(id):
    c = DB.execute(
            "SELECT name FROM builds_to_send_result "
            "WHERE id=? ", (id,))
    return c.fetchone()

def rows_in_table():
    c = DB.execute(
        "SELECT count(*) FROM builds_to_send_result")
    rows = c.fetchone()[0]
    if not rows:
        return 0
    else:
        return rows

def get_latest_build_by_path(path):
    c = DB.execute(
            "SELECT * FROM builds "
            "WHERE path=? "
            "ORDER BY date DESC LIMIT 1", (path,))
    return c.fetchone()

def iter_builds_before_date(date, limit):
    return DB.execute(
        "SELECT * FROM builds "
        "WHERE date < ? AND date >= "
            "(SELECT min(date) FROM "
                "(SELECT date FROM builds "
                    "WHERE date < ? "
                    "ORDER BY date DESC LIMIT ?))",
        (date, date, limit))

def get_build(rowid):
    c = DB.execute("SELECT * FROM builds WHERE id=?", (rowid,))
    return c.fetchone()

def update_build_date(rowid, date):
    DB.execute("UPDATE builds SET date=? WHERE id=?", (date, rowid))

def delete_build_after_send(name):
    DB.execute("DELETE FROM builds_to_send_result WHERE name=?", (name, ))
    DB.commit()

## RESULTS

def create_result(build_id, test_suite_id, machine_group_id, start_date, contact):
    print("contact in db.create_result: ", contact)
    c = DB.execute(
        "INSERT INTO results "
            "(build_id, machine_group_id, test_suite_id, start_date, contact) "
        "VALUES (?, ?, ?, ?, ?)",
        (build_id, machine_group_id, test_suite_id, start_date, contact))
    return c.lastrowid

def get_result(rowid):
    c = DB.execute(
        "SELECT *, "
            "(SELECT name FROM builds WHERE id=build_id) AS build_name "
        "FROM results WHERE id=?", (rowid,))
    return c.fetchone()

def get_status(rowid):
    failed = DB.execute(
        "SELECT COUNT(*) as count "
        "FROM result_items WHERE result_id=? AND test_passed=0", (rowid,)).fetchone()["count"]

    total = DB.execute(
        "SELECT COUNT(*) as count "
        "FROM result_items WHERE result_id=?", (rowid,)).fetchone()["count"]

    if failed is None :
        failed = 0

    if total is None:
        total = 0

    return "failed " + str(failed) + " from " + str(total)

def total_scripts_by_result_id(rowid):
    total = DB.execute(
        "SELECT COUNT(*) as count "
        "FROM result_items WHERE result_id=?", (rowid,)).fetchone()["count"]

    return total

def iter_results_before_date(date, limit):
    return DB.execute(
        "SELECT results.*, "
            "builds.name AS build_name "
        "FROM results JOIN builds "
        "ON results.build_id == builds.id "
        "WHERE results.start_date < ? "
        "AND results.start_date >= "
            "(SELECT min(start_date) FROM "
                "(SELECT start_date FROM results "
                    "WHERE start_date < ? "
                    "ORDER BY start_date DESC LIMIT ?))",
        (date, date, limit))

def iter_results_by_build(build_id):
    return DB.execute(
        "SELECT results.*, builds.name AS build_name "
        "FROM results JOIN builds "
        "ON results.build_id == builds.id "
        "WHERE builds.id=?",
        (build_id,))

def update_result_end_date(rowid, end_date):
    DB.execute("UPDATE results SET end_date=? WHERE id=?", (end_date, rowid))

def update_result_log_path(rowid, log_path):
    DB.execute("UPDATE results SET log_path=? WHERE id=?", (log_path, rowid))

def delete_result(rowid):
    DB.execute("DELETE FROM results WHERE id=?", (rowid,))
    DB.commit()

## TEST_RUNS

def create_test_run(result_id, machine_id, log_path, start_date, end_date):
    c = DB.execute(
        "INSERT INTO test_runs "
            "(result_id, machine_id, log_path, start_date, end_date) "
        "VALUES (?, ?, ?, ?, ?)",
        (result_id, machine_id, log_path, start_date, end_date))
    return c.lastrowid

def get_test_run(rowid):
    c = DB.execute("SELECT * FROM test_runs WHERE id=?", (rowid,))
    return c.fetchone()

def iter_test_runs_by_result(result_id):
    return DB.execute(
            "SELECT * FROM test_runs WHERE result_id=?", (result_id,))

def update_test_run_end_date(rowid, end_date):
    DB.execute("UPDATE test_runs SET end_date=? WHERE id=?", (end_date, rowid))

## RESULT_ITEMS

def create_result_item(result_id, test_name, test_passed):
    c = DB.execute(
            "INSERT INTO result_items (result_id, test_name, test_passed) "
            "VALUES (?, ?, ?)", (result_id, test_name, test_passed))
    return c.lastrowid

def iter_result_items_by_result(result_id):
    return DB.execute(
        "SELECT * FROM result_items WHERE result_id=?", (result_id,))

def update_result_item(rowid, test_name, test_passed):
    DB.execute("UPDATE result_items SET test_name=?, test_passed=? "
               "WHERE id=?", (test_name, test_passed, rowid))
    
def update_item_by_result_id(result_id, test_name, test_passed):
    try:
        DB.execute(
            "UPDATE result_items SET test_passed=? "
            "WHERE result_id=? AND test_name=?", (test_passed, result_id, test_name))
        DB.commit()
    except sqlite3.Error as e:
        print("Error while executing request ", e)
        message = "db, line 536 result_id {} \n{}".format(result_id, str(e))
        save_log(message, suffix="some info")

## CONTACTS

def create_contact(name):
    c = DB.execute("INSERT INTO contacts (name) VALUES (?)", (name,))
    return c.lastrowid

def iter_contacts():
    return DB.execute("SELECT * FROM contacts")

def update_contact(rowid, name):
    DB.execute("UPDATE contacts SET name=? WHERE id=?", (name, rowid))

def delete_contact(rowid):
    DB.execute("DELETE FROM contacts WHERE id=?", (rowid, ))

## CONTACT_ASSIGNMENTS

def create_contact_assignment(contact_id, test_suite_id, test_name_pattern):
    c = DB.execute(
        "INSERT INTO contact_assignments "
            "(contact_id, test_suite_id, test_name_pattern) "
        "VALUES (?, ?, ?)",
        (contact_id, test_suite_id, test_name_pattern))
    return c.lastrowid

def iter_contact_assignments():
    return DB.execute("SELECT * FROM contact_assignments")

def update_contact_assignment(rowid, contact_id, test_suite_id, test_name_pattern):
    DB.execute(
        "UPDATE contact_assignments "
            "SET contact_id=?, test_suite_id=?, test_name_pattern=? WHERE id=?",
        (contact_id, test_suite_id, test_name_pattern, rowid))

def delete_contact_assignment(rowid):
    DB.execute("DELETE FROM contact_assignments WHERE id=?", (rowid, ))

## TELEGRAM BOT

def get_latest_build_by_folder_path(path):
    c = DB.execute(
            "SELECT * FROM builds "
            "WHERE path LIKE ? "
            "ORDER BY date DESC LIMIT 1", ('{}%'.format(path),))
    return c.fetchall()

def add_contact_telegram_id(id, name):
    try:
        DB.execute("INSERT INTO contact_telegram (contact_id, contact_name) " "VALUES (?,?)", (id, name, ))
        DB.commit()
        print('Contact with id=%s has been added to contact_telegram table succesfully'%(id))
    except:
        print("Error to insert. Contact with id=%s has not been added to contact_telegram table"%(id))

def check_contact_id_in_contact_telegram_table(id):
    c = DB.execute("SELECT rowid FROM contact_telegram WHERE contact_id = ?", (id,))
    data=c.fetchone()
    if data:
        print('Component %s was founded with rowid %s'%(id,data['id']))
    else:
        print('There is no component named %s'%id)
    return data

def delete_contact_telegram_id(id):
    try:
        DB.execute("DELETE FROM contact_telegram WHERE contact_id=?", (id, ))
        DB.commit()
        print('User with id=%s was deleted from contact_telegram table succesfully'%(id))
    except:
        print("Some error was appears while deleting contact from contact_telegram table")

def get_contact_telegram_id():
    c = DB.execute("SELECT contact_id FROM contact_telegram")
    return c.fetchall()

## OTHER
def get_build_id_by_name(name):
    c = DB.execute(
            "SELECT id FROM builds "
            "WHERE name LIKE ? "
            "ORDER BY date ASC LIMIT 1", ('{}%'.format(name),))
    return c.fetchone()

def get_results_id(build_id):
    c = DB.execute(
            "SELECT id FROM results "
            "WHERE build_id=? "
            "ORDER BY end_date ASC LIMIT 1", (build_id,))
    return c.fetchone()
    # return c.fetchall()

def all_scripts_by_result(id):
    scripts = []
    total = DB.execute(
        "SELECT * "
        "FROM result_items WHERE result_id=?", (id,)).fetchall()
    for item in total:
        scripts.append(item["test_name"])
    return scripts

def testers():
    testers = []
    result = DB.execute("SELECT * FROM contacts").fetchall()
    for item in result:
        testers.append(item["name"])
    return testers

def fstatus(rowid, test_name):
    status_res = DB.execute(
        "SELECT test_passed "
        "FROM result_items WHERE result_id=? AND test_name=?", (rowid, test_name)).fetchone()['test_passed']
    return status_res

def build_by_result_id(result_id):
    build_id = DB.execute(
        "SELECT build_id "
        "FROM results WHERE id=?", (result_id,)).fetchone()['build_id']
    # builds_id = []
    # for res in results:
    #     builds_id.append(res['build_id'])
    print("build_id: ", build_id)
    build = DB.execute(
        "SELECT name "
        "FROM builds WHERE id=?", (build_id,)).fetchone()['name']
    print("build: ", build)
    pattern = r"(\d{2}-\d{1,2}-\d{1,2}\(\d+\))"
    if (not build.endswith(".lnk")):
        match = re.search(pattern, build)
        if match:
            return match[0]

# Recoloring re-running scripts by build_id

def log_date(build_id, contact="Main Log"):
    c = DB.execute(
            "SELECT start_date, id FROM results "
            "WHERE build_id=? AND contact LIKE ? "
            "ORDER BY end_date ASC ", (build_id, contact + "%"))
    return c.fetchall()

def get_all_reruns(build_id, contact="Main Log"):
    c = DB.execute(
            "SELECT contact, id, start_date FROM results "
            "WHERE build_id=? AND contact!=?"
            "ORDER BY end_date ASC", (build_id, contact))
    return c.fetchall()

def result_by_id(res_id):
    print("id: ", res_id)
    c = DB.execute(
            "SELECT test_name, test_passed FROM result_items "
            "WHERE result_id=?", (res_id,))
    return c.fetchall()

def build_name_by_id(id):
    c = DB.execute(
        "SELECT name FROM builds "
        "WHERE id=?", (id,))
    return c.fetchone()

def main_build_id(result_id):
    c = DB.execute(
        "SELECT build_id FROM results "
        "WHERE id=?", (result_id,))
    return c.fetchone()

def all_not_main_logs():
    c = DB.execute(
            "SELECT id, end_date, contact FROM results "
            "WHERE contact IS NULL OR (contact <> 'Main Log')")
    return c.fetchall()

def contacts_by_build_id(build_id):
    c = DB.execute(
        "SELECT contact, id FROM results "
        "WHERE build_id=?", (build_id,))
    return c.fetchall()

def contact_by_result_id(result_id):
    c = DB.execute(
        "SELECT contact FROM results "
        "WHERE id=?", (result_id,))
    return c.fetchone()

## Statistics

def tester_statistics(result_id):
    dict = {}
    all_scripts = all_scripts_by_result(result_id)
    total_scripts = total_scripts_by_result_id(result_id)
    count = 0
    tester_list = testers()
    for tester in tester_list:
        all = 0
        passed = 0
        for script in all_scripts:
            if(script.find(tester) != -1):
                status = fstatus(result_id, script)
                passed = passed + 1 if status==1 else passed
                count += 1
                all += 1
        dict[tester] = [all, passed]
    return [total_scripts, dict]

def add_build_to_statistics(build, date, total):
    c = DB.execute(
        "INSERT INTO tester_statistics (build, date, total) VALUES (?,?,?)", 
        (build, date, total, ))
    DB.commit()
    return c.fetchone()

def update_statistics(column_name, data, build):
    cursor = DB.execute("PRAGMA table_info(tester_statistics)")
    columns = [row[1] for row in cursor.fetchall()]
    if column_name not in columns:
         print(f"No column {column_name}")
         return None
    c = DB.execute(
        f"UPDATE tester_statistics SET {column_name} = ? WHERE build=?",
        (data, build, ))
    DB.commit()
    return c.fetchone()
    
       

def get_statistics():
    return  DB.execute("SELECT * FROM tester_statistics")

def all_builds():
    return DB.execute("SELECT * FROM tester_statistics")
