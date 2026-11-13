(ns metabase.test.data.hardcharger
  "Test extensions that let Metabase's own driver test suite load its datasets into a Hardcharger server and run
  against it.

  Every test dataset is a catalog of its own, attached in memory (`ATTACH ':memory:' AS \"test-data\"`), and a test
  database connects with that catalog as its Catalog setting, so datasets are isolated the way separate databases
  are on other servers. The server is named by MB_HARDCHARGER_TEST_HOST, _PORT, _USER and _PASSWORD; without a
  password the tests fail, they never skip."
  (:require
   [clojure.java.jdbc :as jdbc]
   [metabase.driver :as driver]
   [metabase.driver.hardcharger]
   [metabase.driver.sql-jdbc.connection :as sql-jdbc.conn]
   [metabase.driver.sql-jdbc.execute :as sql-jdbc.execute]
   [metabase.driver.sql.util :as sql.u]
   [metabase.test.data.interface :as tx]
   [metabase.test.data.sql :as sql.tx]
   [metabase.test.data.sql-jdbc :as sql-jdbc.tx]
   [metabase.test.data.sql-jdbc.load-data :as load-data]
   [metabase.test.data.sql.ddl :as ddl]
   [metabase.util.log :as log]))

(set! *warn-on-reflection* true)

(comment metabase.driver.hardcharger/keep-me)

(sql-jdbc.tx/add-test-extensions! :hardcharger)

;;; ------------------------------------------------ Connecting ------------------------------------------------

(defmethod tx/dbdef->connection-details :hardcharger
  [_driver context {:keys [database-name]}]
  (merge
   {:host     (tx/db-test-env-var :hardcharger :host "127.0.0.1")
    :port     (parse-long (str (tx/db-test-env-var :hardcharger :port "31337")))
    :user     (tx/db-test-env-var :hardcharger :user "hardcharger")
    ;; no default: a run without a server to test against fails here
    :password (tx/db-test-env-var-or-throw :hardcharger :password)
    :tls      false}
   (when (= context :db)
     {:catalog database-name})))

(defmethod tx/bad-connection-details :hardcharger
  [driver]
  (assoc (tx/dbdef->connection-details driver :server nil) :password "not-the-password"))

(defn- server-spec []
  (sql-jdbc.conn/connection-details->spec :hardcharger (tx/dbdef->connection-details :hardcharger :server nil)))

;;; -------------------------------------------- Creating databases --------------------------------------------

(defn- quote-catalog [database-name]
  (sql.u/quote-name :hardcharger :database database-name))

;; A dataset is an in-memory catalog of the server: it lasts until the server stops, which is what the suite's
;; "is this dataset loaded already?" check is for.
(defmethod sql.tx/create-db-sql :hardcharger
  [_driver {:keys [database-name]}]
  (format "ATTACH ':memory:' AS %s" (quote-catalog database-name)))

(defmethod sql.tx/drop-db-if-exists-sql :hardcharger
  [_driver {:keys [database-name]}]
  (format "DETACH DATABASE IF EXISTS %s" (quote-catalog database-name)))

(defmethod tx/dataset-already-loaded? :hardcharger
  [_driver {:keys [database-name table-definitions]}]
  (let [table-name (:table-name (first table-definitions))]
    (boolean
     (seq (jdbc/query (server-spec)
                      ["SELECT 1 FROM information_schema.tables WHERE table_catalog = ? AND table_name = ?"
                       database-name table-name])))))

(defmethod sql.tx/session-schema :hardcharger [_driver] "main")

;;; ------------------------------------------------- Tables -------------------------------------------------

(doseq [[base-type sql-type] {:type/BigInteger          "BIGINT"
                              :type/Boolean             "BOOLEAN"
                              :type/Char                "VARCHAR"
                              :type/Date                "DATE"
                              :type/DateTime            "TIMESTAMP"
                              :type/DateTimeWithTZ      "TIMESTAMP WITH TIME ZONE"
                              :type/DateTimeWithLocalTZ "TIMESTAMP WITH TIME ZONE"
                              :type/Decimal             "DECIMAL(24,6)"
                              :type/Float               "DOUBLE"
                              :type/Integer             "INTEGER"
                              :type/JSON                "JSON"
                              :type/Text                "VARCHAR"
                              :type/Time                "TIME"
                              :type/UUID                "UUID"}]
  (defmethod sql.tx/field-base-type->sql-type [:hardcharger base-type] [_driver _base-type] sql-type))

;; DuckDB has no auto-incrementing column type (a sequence default is the idiom), so the loader gives each row
;; its id.
(defmethod sql.tx/pk-sql-type :hardcharger [_driver] "INTEGER")

(defmethod load-data/row-xform :hardcharger
  [_driver _dbdef tabledef]
  (load-data/maybe-add-ids-xform tabledef))

;; DuckDB cannot add a foreign key to an existing table (ALTER TABLE ... ADD FOREIGN KEY is not implemented), so
;; the constraints go into CREATE TABLE, and the tables are created and loaded parents first.
(defmethod sql.tx/add-fk-sql :hardcharger [& _] nil)

(defn- foreign-key-clauses [driver dbdef {:keys [table-name field-definitions]}]
  (for [{:keys [fk field-name]} field-definitions
        :when                    fk
        :let                     [dest-table (name fk)
                                  pk-names   (sql.tx/fielddefs->pk-field-names
                                              (:field-definitions (sql.tx/get-tabledef dbdef dest-table)))]
        ;; a table that points at itself would need its rows loaded in dependency order
        :when                    (not= dest-table table-name)]
    (format "FOREIGN KEY (%s) REFERENCES %s (%s)"
            (sql.tx/format-and-quote-field-name driver field-name)
            (sql.tx/qualify-and-quote driver (:database-name dbdef) dest-table)
            (sql.tx/format-and-quote-field-name driver (or (first pk-names) (sql.tx/pk-field-name driver))))))

(defmethod sql.tx/create-table-sql :hardcharger
  [driver dbdef tabledef]
  (let [sql ((get-method sql.tx/create-table-sql :sql/test-extensions) driver dbdef tabledef)
        fks (foreign-key-clauses driver dbdef tabledef)]
    (if (empty? fks)
      sql
      ;; "... PRIMARY KEY (id)) ;" -> "... PRIMARY KEY (id), FOREIGN KEY ...) ;"
      (let [end (.lastIndexOf ^String sql ")")]
        (str (subs sql 0 end) ", " (clojure.string/join ", " fks) (subs sql end))))))

(defn- parents-first
  "`tabledefs` ordered so that every table follows the tables its foreign keys point at. Tables in a cycle keep
  their order (and DuckDB then refuses the first of them, which is the right failure)."
  [tabledefs]
  (let [by-name (into {} (map (juxt :table-name identity)) tabledefs)
        parents (fn [tabledef]
                  (into #{}
                        (comp (keep :fk) (map name) (remove #{(:table-name tabledef)}) (filter by-name))
                        (:field-definitions tabledef)))]
    (loop [remaining tabledefs, done [], placed #{}]
      (if (empty? remaining)
        done
        (let [ready (filter #(every? placed (parents %)) remaining)
              batch (if (seq ready) ready [(first remaining)])
              names (into #{} (map :table-name) batch)]
          (recur (remove #(names (:table-name %)) remaining)
                 (into done batch)
                 (into placed names)))))))

(defmethod tx/create-db! :hardcharger
  [driver dbdef & options]
  (apply load-data/create-db! driver (update dbdef :table-definitions parents-first) options))

(defmethod sql.tx/standalone-column-comment-sql :hardcharger [& args]
  (apply sql.tx/standard-standalone-column-comment-sql args))

(defmethod sql.tx/standalone-table-comment-sql :hardcharger [& args]
  (apply sql.tx/standard-standalone-table-comment-sql args))

;;; ----------------------------------------------- Expectations -----------------------------------------------

;; ORDER BY ... ASC puts NULLs last
(defmethod tx/sorts-nil-first? :hardcharger [_driver _base-type] false)

;; COUNT is a BIGINT, and SUM over an integer column a HUGEINT (read as a whole number)
(defmethod tx/aggregate-column-info :hardcharger
  ([driver ag-type]
   (merge
    ((get-method tx/aggregate-column-info ::tx/test-extensions) driver ag-type)
    (when (#{:count :cum-count} ag-type)
      {:base_type :type/BigInteger})))
  ([driver ag-type field]
   (merge
    ((get-method tx/aggregate-column-info ::tx/test-extensions) driver ag-type field)
    (when (#{:count :cum-count} ag-type)
      {:base_type :type/BigInteger})
    (when (and (#{:sum :cum-sum} ag-type)
               (isa? (:base_type field) :type/Integer))
      {:base_type :type/BigInteger}))))

(defmethod driver/database-supports? [:hardcharger :test/time-type] [_driver _feature _database] true)

(comment log/keep-me sql-jdbc.execute/keep-me ddl/keep-me)
