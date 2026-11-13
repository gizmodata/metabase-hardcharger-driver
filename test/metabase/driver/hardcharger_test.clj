(ns ^:mb/driver-tests metabase.driver.hardcharger-test
  "The driver's own tests, beside Metabase's driver test suite: how connection details become JDBC properties (no
  server needed), and what only this driver does against a real Hardcharger server (types, the session's time zone,
  cancelling, time limits, errors)."
  (:require
   [clojure.core.async :as a]
   [clojure.java.jdbc :as jdbc]
   [clojure.string :as str]
   [clojure.test :refer :all]
   [metabase.driver :as driver]
   [metabase.driver.hardcharger]
   [metabase.driver.settings :as driver.settings]
   [metabase.driver.sql-jdbc.connection :as sql-jdbc.conn]
   [metabase.driver.sql-jdbc.execute :as sql-jdbc.execute]
   [metabase.driver.sql-jdbc.sync :as sql-jdbc.sync]
   [metabase.driver.sql.query-processor :as sql.qp]
   [metabase.driver.util :as driver.u]
   [metabase.lib.core :as lib]
   [metabase.lib.metadata :as lib.metadata]
   [metabase.query-processor :as qp]
   [metabase.query-processor.pipeline :as qp.pipeline]
   [metabase.sync.core :as sync]
   [metabase.test :as mt]
   [metabase.test.data.interface :as tx]
   [metabase.util :as u]
   [toucan2.core :as t2])
  (:import
   (java.time LocalDate LocalDateTime LocalTime OffsetDateTime OffsetTime ZoneOffset)))

(set! *warn-on-reflection* true)

(comment metabase.driver.hardcharger/keep-me)

;;; +----------------------------------------------------------------------------------------------------------------+
;;; |                                     Connection details (no server needed)                                      |
;;; +----------------------------------------------------------------------------------------------------------------+

(defn- spec [details]
  (sql-jdbc.conn/connection-details->spec :hardcharger details))

(deftest ^:parallel connection-spec-url-test
  (testing "only the host, the port and the catalog are in the URL"
    (is (= "//db.example.com:31337/lake"
           (:subname (spec {:host "db.example.com", :port 31337, :catalog "lake", :user "u", :password "p"})))))
  (testing "without a port the JDBC driver picks one (31337, or 443 on a GizmoData Cloud host)"
    (is (= "//db.example.com" (:subname (spec {:host "db.example.com"}))))
    (is (= "//db.example.com" (:subname (spec {:host "db.example.com", :port ""})))))
  (testing "a port given as text, as the API may send it"
    (is (= "//h:8443" (:subname (spec {:host "h", :port "8443"})))))
  (testing "an IPv6 literal goes in brackets"
    (is (= "//[::1]:31337" (:subname (spec {:host "::1", :port 31337})))))
  (testing "a catalog name is percent-encoded"
    (is (= "//h/my%20lake%3A1" (:subname (spec {:host "h", :catalog "my lake:1"})))))
  (testing "a port out of range is refused"
    (is (thrown-with-msg? clojure.lang.ExceptionInfo #"out of range" (spec {:host "h", :port 70000}))))
  (testing "the JDBC driver and URL scheme"
    (is (=? {:classname "com.gizmodata.hardcharger.jdbc.HardchargerDriver", :subprotocol "hardcharger"}
            (spec {:host "h"})))))

(deftest ^:parallel connection-spec-tls-test
  (testing "TLS is on unless it is turned off"
    (is (= "true" (get (spec {:host "h"}) "useEncryption")))
    (is (= "true" (get (spec {:host "h", :tls true}) "useEncryption")))
    (is (= "false" (get (spec {:host "h", :tls false}) "useEncryption"))))
  (testing "certificate verification is on unless it is skipped, and skipping means nothing without TLS"
    (is (not (contains? (spec {:host "h"}) "disableCertificateVerification")))
    (is (= "true" (get (spec {:host "h", :tls-skip-verify true}) "disableCertificateVerification")))
    (is (not (contains? (spec {:host "h", :tls false, :tls-skip-verify true}) "disableCertificateVerification")))))

(deftest ^:parallel connection-spec-credentials-test
  (testing "user name and password"
    (is (=? {"user" "alice", "password" "s3:cr@t;&?# "}
            (spec {:host "h", :user "alice", :password "s3:cr@t;&?# "})))
    (is (= "//h" (:subname (spec {:host "h", :user "alice", :password "s3:cr@t;&?#"})))
        "a password never reaches the URL"))
  (testing "an access token, when that is the chosen sign-in"
    (let [s (spec {:host "h", :use-token true, :token-value "tok.en", :user "ignored", :password "ignored"})]
      (is (= "tok.en" (get s "token")))
      (is (not (contains? s "user")))
      (is (not (contains? s "password")))))
  (testing "a token left in the details is not sent when the sign-in is by password"
    (let [s (spec {:host "h", :use-token false, :token-value "stale", :user "u", :password "p"})]
      (is (not (contains? s "token")))
      (is (= "u" (get s "user")))))
  (testing "token sign-in without a token says what to do"
    (is (thrown-with-msg? clojure.lang.ExceptionInfo #"Enter an access token" (spec {:host "h", :use-token true}))))
  (testing "no browser sign-in, whatever the details say"
    (is (not (contains? (spec {:host "h", :authType "oauth_browser", :auth-type "oauth_browser"}) "authType")))))

(deftest ^:parallel connection-spec-is-stable-test
  (testing "the same details give an equal spec with an equal hash: Metabase compares hashes to decide whether its
           connection pool is stale"
    (let [details {:host "h", :port 1, :user "u", :password "p", :additional-options "async=true"}]
      (is (= (spec details) (spec details)))
      (is (= (hash (spec details)) (hash (spec details))))
      (is (every? string? (vals (spec details)))))))

(deftest ^:parallel additional-options-test
  (testing "allowed options, separated by & or ;, in any letter case, become JDBC properties in the driver's spelling"
    (is (=? {"resultPartitions" "8", "networkTimeout" "600000", "ipcCompression" "zstd", "basePath" "/chargewire"}
            (spec {:host "h"
                   :additional-options "resultpartitions=8&networkTimeout=600000;IPCCOMPRESSION=zstd&basePath=%2Fchargewire"}))))
  (testing "a leading ? is fine, and empty input is no options"
    (is (= "true" (get (spec {:host "h", :additional-options "?async=true"}) "async")))
    (is (= (spec {:host "h"}) (spec {:host "h", :additional-options "  "}))))
  (testing "anything else is refused, by name"
    (doseq [option ["oauthBrowser=/usr/bin/id" "authType=oauth_browser" "tlsRootCerts=/etc/passwd"
                    "trustStore=/tmp/x" "clientKey=/tmp/k" "clientCertificate=/tmp/c" "user=root" "password=x"
                    "token=x" "subjectToken=x" "disableCertificateVerification=true" "useEncryption=false"
                    "catalog=other" "noSuchOption=1"]]
      (testing option
        (is (thrown-with-msg? clojure.lang.ExceptionInfo #"Unsupported additional JDBC option"
                              (spec {:host "h", :additional-options option}))))))
  (testing "the refusal names the option and never its value"
    (let [e (try (spec {:host "h", :additional-options "password=hunter2-secret"}) (catch Exception e e))]
      (is (str/includes? (ex-message e) "\"password\""))
      (is (not (str/includes? (ex-message e) "hunter2-secret")))
      (is (not (str/includes? (pr-str (ex-data e)) "hunter2-secret")))))
  (testing "an option without a value is refused"
    (is (thrown-with-msg? clojure.lang.ExceptionInfo #"needs a value" (spec {:host "h", :additional-options "async"})))))

(deftest ^:parallel disallowed-connection-parameters-test
  (testing "Metabase's own check is told the dangerous parameters too"
    (let [disallowed (set (driver/disallowed-connection-parameters :hardcharger))]
      (is (every? disallowed ["oauthBrowser" "authType" "tlsRootCerts" "trustStore" "clientKey" "clientCertificate"]))))
  (testing "and that check passes this driver's own details, whose keys must not look like one of them"
    (is (nil? (driver.u/validate-connection-parameters!
               :hardcharger
               {:host "h", :port 31337, :tls true, :use-token true, :token-value "t", :catalog "lake"
                :tls-ca-certificate-value "pem", :mtls-certificate-value "pem", :mtls-key-value "pem"
                :mtls-sign-in false, :tls-skip-verify false, :additional-options "async=true"})))))

(deftest ^:parallel humanize-connection-error-message-test
  (are [message expected] (= expected (driver/humanize-connection-error-message :hardcharger [message]))
    "the user name or password is not correct [invalid_credentials, request r_x]" :username-or-password-incorrect
    "Cannot connect to http://127.0.0.1:1/chargewire: ConnectException"           :cannot-connect-check-host-and-port)
  (is (re-find #"does not use TLS"
               (driver/humanize-connection-error-message
                :hardcharger
                ["TLS failure with https://h:1/chargewire: Unrecognized SSL message, plaintext connection?"])))
  (is (re-find #"access token"
               (driver/humanize-connection-error-message
                :hardcharger ["a valid access token is required [invalid_token, request r_x]"])))
  (testing "a message it does not know is passed on as it is"
    (is (= "something else" (driver/humanize-connection-error-message :hardcharger ["something else"])))))

;;; +----------------------------------------------------------------------------------------------------------------+
;;; |                                               Types (no server)                                                |
;;; +----------------------------------------------------------------------------------------------------------------+

(deftest ^:parallel database-type->base-type-test
  (are [database-type base-type] (= base-type (sql-jdbc.sync/database-type->base-type :hardcharger database-type))
    ;; as the catalog names them
    "BOOLEAN"                          :type/Boolean
    "TINYINT"                          :type/Integer
    "SMALLINT"                         :type/Integer
    "INTEGER"                          :type/Integer
    "BIGINT"                           :type/BigInteger
    "HUGEINT"                          :type/BigInteger
    "UTINYINT"                         :type/Integer
    "USMALLINT"                        :type/Integer
    "UINTEGER"                         :type/BigInteger
    "UBIGINT"                          :type/BigInteger
    "UHUGEINT"                         :type/BigInteger
    "FLOAT"                            :type/Float
    "DOUBLE"                           :type/Float
    "DECIMAL(18,3)"                    :type/Decimal
    "DECIMAL(38,10)"                   :type/Decimal
    "DECIMAL(18,0)"                    :type/BigInteger
    "VARCHAR"                          :type/Text
    "UUID"                             :type/UUID
    "DATE"                             :type/Date
    "TIME"                             :type/Time
    "TIME_NS"                          :type/Time
    "TIME WITH TIME ZONE"              :type/Time
    "TIMESTAMP"                        :type/DateTime
    "TIMESTAMP_S"                      :type/DateTime
    "TIMESTAMP_MS"                     :type/DateTime
    "TIMESTAMP_NS"                     :type/DateTime
    "TIMESTAMP WITH TIME ZONE"         :type/DateTimeWithLocalTZ
    "JSON"                             :type/JSON
    "ENUM('sad', 'ok', 'happy')"       :type/Text
    "INTEGER[]"                        :type/Array
    "INTEGER[3]"                       :type/Array
    "STRUCT(a INTEGER, b VARCHAR)[]"   :type/Array
    "STRUCT(a INTEGER, b VARCHAR)"     :type/Dictionary
    "MAP(VARCHAR, INTEGER)"            :type/Dictionary
    "UNION(num INTEGER, str VARCHAR)"  :type/*
    "VARIANT"                          :type/*
    "INTERVAL"                         :type/*
    "BLOB"                             :type/*
    "BIT"                              :type/*
    "BIGNUM"                           :type/*
    "GEOMETRY"                         :type/*
    ;; as a result set names them
    "REAL"                             :type/Float
    "DECIMAL(38,0)"                    :type/BigInteger
    "VARBINARY"                        :type/*
    "STRUCT"                           :type/Dictionary
    "MAP"                              :type/Dictionary
    "UNION"                            :type/*)
  (testing "a nested type is typed as a whole, not by a scalar type name inside it"
    (are [database-type base-type] (= base-type (sql-jdbc.sync/database-type->base-type :hardcharger database-type))
      "STRUCT(started_at TIMESTAMP, n INTEGER)" :type/Dictionary
      "ENUM('DATE', 'STRUCT(')"                  :type/Text
      "MAP(DATE, TIMESTAMP)"                     :type/Dictionary))
  (testing "keywords too, as Metabase passes result-set type names"
    (is (= :type/BigInteger (sql-jdbc.sync/database-type->base-type :hardcharger :BIGINT)))))

(deftest ^:parallel inline-temporal-literals-test
  (testing "DuckDB reads an offset only directly after the time, never after a space"
    (are [value sql] (= sql (sql.qp/inline-value :hardcharger value))
      (LocalDate/of 2026 11 13)                                             "DATE '2026-11-13'"
      (LocalTime/of 13 14 15 123456000)                                     "TIME '13:14:15.123456'"
      (LocalDateTime/of 2026 11 13 13 14 15 123456000)                      "TIMESTAMP '2026-11-13 13:14:15.123456'"
      (OffsetDateTime/of 2026 11 13 13 14 15 0 (ZoneOffset/ofHours -7))     "TIMESTAMPTZ '2026-11-13 13:14:15.000000-07:00'"
      (OffsetDateTime/of 2026 11 13 13 14 15 0 ZoneOffset/UTC)              "TIMESTAMPTZ '2026-11-13 13:14:15.000000+00:00'"
      (OffsetTime/of 13 14 15 0 (ZoneOffset/ofHoursMinutes 5 30))           "TIMETZ '13:14:15.000000+05:30'")))

;;; +----------------------------------------------------------------------------------------------------------------+
;;; |                                          Against a Hardcharger server                                          |
;;; +----------------------------------------------------------------------------------------------------------------+

(defn- metadata-db
  "The test database as drivers are handed it: its Lib metadata."
  []
  (lib.metadata/database (mt/metadata-provider)))

(defn- native-rows
  "Rows of a native query against the test database, as the driver read them (no formatting)."
  [sql]
  (mt/rows (qp/process-query (assoc (mt/native-query {:query sql}) :middleware {:format-rows? false}))))

(defn- native-cols [sql]
  (mt/cols (qp/process-query (mt/native-query {:query sql}))))

(deftest ^:parallel literals-run-on-the-server-test
  (mt/test-driver :hardcharger
    (testing "every inlined temporal literal is SQL the server accepts, and means the value it was made from"
      (is (= [[true true true true true]]
             (native-rows
              (format "SELECT %s = DATE '2026-11-13', %s = TIME '13:14:15.123456', %s = TIMESTAMP '2026-11-13 13:14:15.123456', %s = TIMESTAMPTZ '2026-11-13 20:14:15+00', %s = TIMETZ '13:14:15+05:30'"
                      (sql.qp/inline-value :hardcharger (LocalDate/of 2026 11 13))
                      (sql.qp/inline-value :hardcharger (LocalTime/of 13 14 15 123456000))
                      (sql.qp/inline-value :hardcharger (LocalDateTime/of 2026 11 13 13 14 15 123456000))
                      (sql.qp/inline-value :hardcharger (OffsetDateTime/of 2026 11 13 13 14 15 0 (ZoneOffset/ofHours -7)))
                      (sql.qp/inline-value :hardcharger (OffsetTime/of 13 14 15 0 (ZoneOffset/ofHoursMinutes 5 30))))))))))

(def ^:private every-type-sql
  "One row with a value of every DuckDB type the driver maps."
  (str "SELECT true AS c_bool, 1::TINYINT AS c_tinyint, 2::SMALLINT AS c_smallint, 3::INTEGER AS c_int,"
       " 4::BIGINT AS c_bigint, 12345678901234567890123456789::HUGEINT AS c_hugeint,"
       " 5::UTINYINT AS c_utinyint, 6::USMALLINT AS c_usmallint, 7::UINTEGER AS c_uint,"
       " 18446744073709551615::UBIGINT AS c_ubigint, 12345678901234567890123456789::UHUGEINT AS c_uhugeint,"
       " 1.5::FLOAT AS c_float, 2.5::DOUBLE AS c_double, 12345.678::DECIMAL(18,3) AS c_dec, 42::DECIMAL(18,0) AS c_dec0,"
       " 'hello' AS c_varchar, '123e4567-e89b-12d3-a456-426614174000'::UUID AS c_uuid,"
       " DATE '2026-11-13' AS c_date, TIME '13:14:15.123456' AS c_time, TIMETZ '13:14:15+02:00' AS c_timetz,"
       " TIMESTAMP '2026-11-13 13:14:15.123456' AS c_ts, TIMESTAMP_S '2026-11-13 13:14:15' AS c_ts_s,"
       " TIMESTAMP_MS '2026-11-13 13:14:15.123' AS c_ts_ms, TIMESTAMP_NS '2026-11-13 13:14:15.123456789' AS c_ts_ns,"
       " TIMESTAMPTZ '2026-11-13 13:14:15.123456+00' AS c_tstz, INTERVAL '1 year 2 months 3 days 4 hours' AS c_interval,"
       " 'ok'::ENUM('sad', 'ok', 'happy') AS c_enum, [1, 2, 3] AS c_list, {'a': 1, 'b': 'x'} AS c_struct,"
       " MAP {'k': 1} AS c_map, '{\"a\": [1, 2]}'::JSON AS c_json, NULL::INTEGER AS c_null"))

(deftest ^:parallel read-every-type-test
  (mt/test-driver :hardcharger
    (let [[row]  (native-rows every-type-sql)
          cols   (native-cols every-type-sql)
          value  (zipmap (map :name cols) row)
          type-of (zipmap (map :name cols) (map :base_type cols))]
      (testing "values"
        (is (= {"c_bool"     true
                "c_tinyint"  1
                "c_smallint" 2
                "c_int"      3
                "c_bigint"   4
                "c_hugeint"  12345678901234567890123456789N
                "c_utinyint" 5
                "c_usmallint" 6
                "c_uint"     7
                "c_ubigint"  18446744073709551615N
                "c_uhugeint" 12345678901234567890123456789N
                "c_float"    1.5
                "c_double"   2.5
                "c_dec"      12345.678M
                "c_dec0"     42
                "c_varchar"  "hello"
                ;; a UUID arrives as text: see the README's type table
                "c_uuid"     "123e4567-e89b-12d3-a456-426614174000"
                "c_date"     (LocalDate/of 2026 11 13)
                "c_time"     (LocalTime/of 13 14 15 123456000)
                ;; the offset of a TIME WITH TIME ZONE is not sent
                "c_timetz"   (LocalTime/of 13 14 15)
                "c_ts"       (LocalDateTime/of 2026 11 13 13 14 15 123456000)
                "c_ts_s"     (LocalDateTime/of 2026 11 13 13 14 15)
                "c_ts_ms"    (LocalDateTime/of 2026 11 13 13 14 15 123000000)
                "c_ts_ns"    (LocalDateTime/of 2026 11 13 13 14 15 123456789)
                "c_tstz"     (OffsetDateTime/of 2026 11 13 13 14 15 123456000 ZoneOffset/UTC)
                "c_interval" "P1Y2M3DT4H"
                "c_enum"     "ok"
                "c_list"     "[1, 2, 3]"
                "c_struct"   "{\"a\": 1, \"b\": \"x\"}"
                "c_map"      "{\"k\": 1}"
                "c_json"     "{\"a\": [1, 2]}"
                "c_null"     nil}
               (update-vals value #(cond-> % (instance? Float %) double (instance? BigInteger %) bigint)))))
      (testing "a whole number that fits a Long is one, whatever type carried it"
        (is (instance? Long (value "c_dec0")))
        (is (instance? BigInteger (value "c_hugeint"))))
      (testing "base types of the result's columns"
        (is (= {"c_bool" :type/Boolean, "c_int" :type/Integer, "c_bigint" :type/BigInteger,
                "c_hugeint" :type/BigInteger, "c_ubigint" :type/BigInteger, "c_double" :type/Float,
                "c_dec" :type/Decimal, "c_dec0" :type/BigInteger, "c_varchar" :type/Text, "c_date" :type/Date,
                "c_time" :type/Time, "c_ts" :type/DateTime, "c_ts_ns" :type/DateTime,
                "c_tstz" :type/DateTimeWithLocalTZ, "c_list" :type/Array, "c_struct" :type/Dictionary,
                "c_map" :type/Dictionary}
               (select-keys type-of ["c_bool" "c_int" "c_bigint" "c_hugeint" "c_ubigint" "c_double" "c_dec"
                                     "c_dec0" "c_varchar" "c_date" "c_time" "c_ts" "c_ts_ns" "c_tstz" "c_list"
                                     "c_struct" "c_map"])))))))

(deftest ^:parallel hugeint-beyond-38-digits-test
  (mt/test-driver :hardcharger
    (testing "a HUGEINT with more than 38 digits cannot be sent; the server's message says what to do"
      (is (thrown-with-msg? Exception #"CAST it to VARCHAR"
                            (native-rows "SELECT 170141183460469231731687303715884105727::HUGEINT AS big")))
      (is (= [["170141183460469231731687303715884105727"]]
             (native-rows "SELECT 170141183460469231731687303715884105727::HUGEINT::VARCHAR AS big"))))))

(deftest ^:parallel sum-of-integers-is-a-whole-number-test
  (mt/test-driver :hardcharger
    (testing "SUM over an integer column is a HUGEINT in DuckDB; it reads as a whole number"
      (let [[[total]] (mt/rows (qp/process-query (assoc (mt/mbql-query venues {:aggregation [[:sum $price]]})
                                                        :middleware {:format-rows? false})))]
        (is (= 203 total))
        (is (instance? Long total))))))

(deftest ^:parallel temporal-bytes-coercion-test
  (mt/test-driver :hardcharger
    (testing "text held in a BLOB, read as a date and time (Metabase's own datasets for this name the BLOB type per
             in-tree driver, so they cannot be loaded here)"
      (is (= [[(LocalDateTime/of 2019 4 21 16 43 0)]]
             (native-rows
              (first (sql.qp/format-honeysql
                      :hardcharger
                      {:select [[(sql.qp/cast-temporal-byte :hardcharger :Coercion/YYYYMMDDHHMMSSBytes->Temporal
                                                            [:raw "'20190421164300'::BLOB"])
                                 :t]]})))))
      (is (= [[(LocalDateTime/of 2019 4 21 16 43 0)]]
             (native-rows
              (first (sql.qp/format-honeysql
                      :hardcharger
                      {:select [[(sql.qp/cast-temporal-byte :hardcharger :Coercion/ISO8601Bytes->Temporal
                                                            [:raw "'2019-04-21T16:43:00'::BLOB"])
                                 :t]]}))))))))

;;; ------------------------------------------------ Sync ------------------------------------------------

(defn- execute-on-server!
  "Run statements on the test database's catalog, on a connection of its own."
  [& statements]
  (sql-jdbc.execute/do-with-connection-with-options
   :hardcharger (mt/id) {:write? true}
   (fn [^java.sql.Connection conn]
     (doseq [sql statements]
       (with-open [stmt (.createStatement conn)]
         (.execute stmt ^String sql))))))

(deftest sync-every-type-keys-comments-and-views-test
  (mt/test-driver :hardcharger
    (execute-on-server!
     "CREATE SCHEMA IF NOT EXISTS hc_sync_test"
     "DROP VIEW IF EXISTS hc_sync_test.v_parent"
     "DROP TABLE IF EXISTS hc_sync_test.child"
     "DROP TABLE IF EXISTS hc_sync_test.parent"
     "DROP TYPE IF EXISTS hc_sync_test.mood"
     "CREATE TYPE hc_sync_test.mood AS ENUM ('sad', 'ok', 'happy')"
     (str "CREATE TABLE hc_sync_test.parent ("
          " id INTEGER PRIMARY KEY, name VARCHAR NOT NULL, flag BOOLEAN DEFAULT true, big HUGEINT, ubig UBIGINT,"
          " amount DECIMAL(18,3), whole DECIMAL(18,0), ratio DOUBLE, uid UUID, born DATE, at_time TIME,"
          " at_time_tz TIMETZ, created TIMESTAMP, created_s TIMESTAMP_S, created_ms TIMESTAMP_MS,"
          " created_ns TIMESTAMP_NS, seen TIMESTAMPTZ, took INTERVAL, feeling hc_sync_test.mood, tags VARCHAR[],"
          " point STRUCT(x DOUBLE, y DOUBLE), counts MAP(VARCHAR, INTEGER), doc JSON, raw BLOB, anything VARIANT)")
     "COMMENT ON TABLE hc_sync_test.parent IS 'a parent table'"
     "COMMENT ON COLUMN hc_sync_test.parent.name IS 'the name'"
     (str "CREATE TABLE hc_sync_test.child ("
          " id INTEGER, line INTEGER, parent_id INTEGER REFERENCES hc_sync_test.parent (id), PRIMARY KEY (id, line))")
     "CREATE VIEW hc_sync_test.v_parent AS SELECT id, name, seen FROM hc_sync_test.parent")
    (try
      (sync/sync-database! (mt/id) {:scan :schema})
      (let [table  (fn [table-name]
                     (t2/select-one :model/Table :db_id (mt/id) :schema "hc_sync_test" :name table-name :active true))
            fields (fn [table-name]
                     (into {} (map (juxt :name identity))
                           (t2/select :model/Field :table_id (:id (table table-name)) :active true)))
            parent (fields "parent")
            child  (fields "child")]
        (testing "tables, the view and the table comment"
          (is (some? (table "parent")))
          (is (some? (table "child")))
          (is (some? (table "v_parent")))
          (is (= "a parent table" (:description (table "parent")))))
        (testing "base types, with each column's full database type"
          (is (= {"id"         ["INTEGER" :type/Integer]
                  "name"       ["VARCHAR" :type/Text]
                  "flag"       ["BOOLEAN" :type/Boolean]
                  "big"        ["HUGEINT" :type/BigInteger]
                  "ubig"       ["UBIGINT" :type/BigInteger]
                  "amount"     ["DECIMAL(18,3)" :type/Decimal]
                  "whole"      ["DECIMAL(18,0)" :type/BigInteger]
                  "ratio"      ["DOUBLE" :type/Float]
                  "uid"        ["UUID" :type/UUID]
                  "born"       ["DATE" :type/Date]
                  "at_time"    ["TIME" :type/Time]
                  "at_time_tz" ["TIME WITH TIME ZONE" :type/Time]
                  "created"    ["TIMESTAMP" :type/DateTime]
                  "created_s"  ["TIMESTAMP_S" :type/DateTime]
                  "created_ms" ["TIMESTAMP_MS" :type/DateTime]
                  "created_ns" ["TIMESTAMP_NS" :type/DateTime]
                  "seen"       ["TIMESTAMP WITH TIME ZONE" :type/DateTimeWithLocalTZ]
                  "took"       ["INTERVAL" :type/*]
                  "feeling"    ["ENUM('sad', 'ok', 'happy')" :type/Text]
                  "tags"       ["VARCHAR[]" :type/Array]
                  "point"      ["STRUCT(x DOUBLE, y DOUBLE)" :type/Dictionary]
                  "counts"     ["MAP(VARCHAR, INTEGER)" :type/Dictionary]
                  "doc"        ["JSON" :type/JSON]
                  "raw"        ["BLOB" :type/*]
                  "anything"   ["VARIANT" :type/*]}
                 (update-vals parent (juxt :database_type :base_type)))))
        (testing "primary keys, a composite one too"
          (is (= :type/PK (:semantic_type (parent "id"))))
          (is (= :type/PK (:semantic_type (child "id"))))
          (is (= :type/PK (:semantic_type (child "line")))))
        (testing "the foreign key points at the parent's key"
          (is (= :type/FK (:semantic_type (child "parent_id"))))
          (is (= (:id (parent "id")) (:fk_target_field_id (child "parent_id")))))
        (testing "column comment, NOT NULL and default"
          (is (= "the name" (:description (parent "name"))))
          (is (true? (:database_required (parent "name"))))
          (is (false? (:database_is_nullable (parent "name"))))
          (is (false? (:database_required (parent "flag"))) "a column with a default is not required")
          (is (true? (:database_is_nullable (parent "ratio")))))
        (testing "a view's columns have their types"
          (is (= {"id" :type/Integer, "name" :type/Text, "seen" :type/DateTimeWithLocalTZ}
                 (update-vals (fields "v_parent") :base_type)))))
      (testing "table-exists?"
        (is (true? (driver/table-exists? :hardcharger (metadata-db) {:schema "hc_sync_test", :name "parent"})))
        (is (false? (driver/table-exists? :hardcharger (metadata-db) {:schema "hc_sync_test", :name "no_such_table"}))))
      (finally
        (execute-on-server!
         "DROP VIEW IF EXISTS hc_sync_test.v_parent"
         "DROP TABLE IF EXISTS hc_sync_test.child"
         "DROP TABLE IF EXISTS hc_sync_test.parent"
         "DROP TYPE IF EXISTS hc_sync_test.mood"
         "DROP SCHEMA IF EXISTS hc_sync_test")
        (sync/sync-database! (mt/id) {:scan :schema})))))

(deftest schema-filters-and-system-schemas-test
  (mt/test-driver :hardcharger
    (let [database (metadata-db)
          schemas  (fn [db] (into #{} (map :schema) (:tables (driver/describe-database :hardcharger db))))]
      (testing "system schemas are never synced"
        (is (= #{"main"} (schemas database))))
      (testing "an exclusion pattern removes a schema, an inclusion pattern keeps only its matches"
        (is (= #{} (schemas (update database :details assoc
                                    :schema-filters-type "exclusion" :schema-filters-patterns "ma*"))))
        (is (= #{"main"} (schemas (update database :details assoc
                                          :schema-filters-type "inclusion" :schema-filters-patterns "main"))))
        (is (= #{} (schemas (update database :details assoc
                                    :schema-filters-type "inclusion" :schema-filters-patterns "other"))))))))

;;; ---------------------------------------- The session's time zone ----------------------------------------

(deftest session-time-zone-test
  (mt/test-driver :hardcharger
    (let [zone-and-day (fn []
                         (first (native-rows
                                 (str "SELECT current_setting('TimeZone'),"
                                      " CAST(date_trunc('day', TIMESTAMPTZ '2026-01-01 02:00:00+00') AS VARCHAR),"
                                      " TIMESTAMPTZ '2026-01-01 02:00:00+00'"))))
          server-zone  (driver/db-default-timezone :hardcharger (metadata-db))]
      (testing "without a report time zone the session runs in the server's own zone"
        (is (= server-zone (first (zone-and-day)))))
      (testing "with one, the session's TimeZone is that zone: days start there, and instants stay instants"
        (mt/with-temporary-setting-values [report-timezone "America/Los_Angeles"]
          (is (= ["America/Los_Angeles"
                  "2025-12-31 00:00:00-08"
                  (OffsetDateTime/of 2026 1 1 2 0 0 0 ZoneOffset/UTC)]
                 (zone-and-day))))
        (mt/with-temporary-setting-values [report-timezone "Asia/Kolkata"]
          (is (= ["Asia/Kolkata" "2026-01-01 00:00:00+05:30"] (take 2 (zone-and-day))))))
      (testing "and afterwards every pooled connection is back in the server's zone"
        (dotimes [_ 20]
          (is (= server-zone (first (zone-and-day))))))
      (testing "db-default-timezone answers the server's zone even while a report time zone is set"
        (mt/with-temporary-setting-values [report-timezone "Asia/Kolkata"]
          (is (= ["Asia/Kolkata"] (take 1 (zone-and-day))))
          (is (= server-zone (driver/db-default-timezone :hardcharger (metadata-db)))))))))

(deftest zoneless-parameter-against-timestamptz-test
  (mt/test-driver :hardcharger
    (mt/with-temporary-setting-values [report-timezone "Asia/Kolkata"]
      (let [matches (fn [param]
                      (ffirst (mt/rows (qp/process-query
                                        {:database (mt/id)
                                         :type     :native
                                         :native   {:query  "SELECT count(*) FROM (SELECT TIMESTAMPTZ '2026-01-01 00:00:00+05:30' AS ts) t WHERE ts = ?"
                                                    :params [param]}}))))]
        (testing "a date and time without a zone means that time in the report time zone, as the SQL literal does"
          (is (= 1 (matches (LocalDateTime/of 2026 1 1 0 0))))
          (is (= 1 (matches (LocalDate/of 2026 1 1)))))
        (testing "one with an offset is the instant it names"
          (is (= 1 (matches (OffsetDateTime/of 2025 12 31 18 30 0 0 ZoneOffset/UTC))))
          (is (= 0 (matches (OffsetDateTime/of 2026 1 1 0 0 0 0 ZoneOffset/UTC)))))))))

;;; ------------------------------------- Cancelling, time limits, errors -------------------------------------

(def ^:private slow-sql
  "A query that runs for far longer than any of these tests waits."
  "SELECT count(*) FROM range(100000000000) a, range(1000) b")

(deftest cancel-running-query-test
  (mt/test-driver :hardcharger
    (testing "cancelling a question cancels its statement on the server, and the connection is fine afterwards"
      (let [canceled-chan (a/promise-chan)
            timer         (u/start-timer)
            result        (future
                            (try
                              (binding [qp.pipeline/*canceled-chan* canceled-chan]
                                (qp/process-query (mt/native-query {:query slow-sql})))
                              (catch Throwable e e)))]
        (Thread/sleep 1500)
        (is (not (realized? result)) "the slow query is still running: without that this test proves nothing")
        (a/>!! canceled-chan ::cancel)
        (let [outcome (deref result 20000 ::still-running)
              elapsed (u/since-ms timer)]
          (is (not= ::still-running outcome) "the query ended once it was cancelled")
          (is (< elapsed 15000) (str "it ended " elapsed " ms after it started")))
        (is (= [[42]] (native-rows "SELECT 42")))))))

(deftest query-timeout-test
  (mt/test-driver :hardcharger
    (testing "a statement that outlives Metabase's query timeout is stopped by the server"
      (let [timer   (u/start-timer)
            error   (binding [driver.settings/*query-timeout-ms* 2000]
                      (try
                        (qp/process-query (mt/native-query {:query slow-sql}))
                        nil
                        (catch Throwable e e)))
            elapsed (u/since-ms timer)]
        (is (some? error))
        (is (re-find #"(?i)timeout|cancel" (str (ex-message error) " " (some-> error ex-cause ex-message))))
        (is (< elapsed 15000) (str "stopped after " elapsed " ms")))
      (is (= [[42]] (native-rows "SELECT 42"))))))

(deftest ^:parallel errors-are-the-servers-own-test
  (mt/test-driver :hardcharger
    (testing "a SQL error reaches the user as the server wrote it"
      (is (thrown-with-msg? Exception #"syntax error at or near \"SELEC\"" (native-rows "SELEC 1")))
      (is (thrown-with-msg? Exception #"Table with name no_such_table does not exist"
                            (native-rows "SELECT * FROM no_such_table"))))))

(deftest connection-errors-test
  (mt/test-driver :hardcharger
    (let [details (tx/dbdef->connection-details :hardcharger :server nil)
          message (fn [details]
                    (try
                      (driver/can-connect? :hardcharger details)
                      (catch Throwable e
                        (str/join " | " (keep ex-message (take-while some? (iterate ex-cause e)))))))]
      (testing "the right details connect"
        (is (true? (driver/can-connect? :hardcharger details))))
      (testing "a wrong password is refused, and no message repeats it"
        (let [text (message (assoc details :password "wrong-hunter2-secret"))]
          (is (re-find #"user name or password is not correct" text))
          (is (not (str/includes? text "wrong-hunter2-secret")))))
      (testing "a catalog the server does not have"
        (is (re-find #"No catalog" (message (assoc details :catalog "no_such_catalog")))))
      (testing "TLS against a server without it"
        (is (re-find #"plaintext connection" (message (assoc details :tls true))))))))

(deftest read-only-is-a-hint-test
  (mt/test-driver :hardcharger
    (testing "Metabase marks the connections of questions read-only. The JDBC driver takes that as a hint and
             Hardcharger has no read-only session, so a native question can still write if its user may: what stops
             writes is the user's role, or a server started with --read-only. This test states that, so a change
             of it is noticed."
      (sql-jdbc.execute/do-with-connection-with-options
       :hardcharger (mt/id) {:write? false}
       (fn [^java.sql.Connection conn]
         (is (true? (.isReadOnly conn)))
         (jdbc/execute! {:connection conn} ["CREATE OR REPLACE TABLE main.hc_read_only_probe (i INTEGER)"])
         (is (= [{:n 0}] (jdbc/query {:connection conn} ["SELECT count(*) AS n FROM main.hc_read_only_probe"])))
         (jdbc/execute! {:connection conn} ["DROP TABLE main.hc_read_only_probe"]))))))

;;; +----------------------------------------------------------------------------------------------------------------+
;;; |                                        CSV uploads and table transforms                                        |
;;; +----------------------------------------------------------------------------------------------------------------+

;; Metabase's own upload and transform tests run for this driver now that it declares the features. These cover
;; what is this driver's own underneath them.

(deftest ^:parallel writing-features-test
  (testing "uploads and table transforms are offered; the writing features that are not implemented are not"
    (are [feature supported?] (= supported? (driver/database-supports? :hardcharger feature nil))
      :uploads                true
      :transforms/table       true
      :upload-with-auto-pk    false
      :actions                false
      :actions/custom         false
      :actions/data-editing   false
      :persist-models         false
      :transforms/python      false)))

(deftest ^:parallel upload-types-test
  (are [upload-type column-type] (= column-type (driver/upload-type->database-type :hardcharger upload-type))
    :metabase.upload/varchar-255     [:varchar]
    :metabase.upload/text            [:varchar]
    :metabase.upload/int             [:bigint]
    :metabase.upload/float           [:double]
    :metabase.upload/boolean         [:boolean]
    :metabase.upload/date            [:date]
    :metabase.upload/datetime        [:timestamp]
    :metabase.upload/offset-datetime [:timestamp-with-time-zone])
  (testing "what a column may be widened to is closed under itself, as Metabase asks"
    (let [promotions (driver/allowed-promotions :hardcharger)]
      (is (every? (fn [[_from tos]]
                    (every? (fn [to] (every? tos (get promotions to))) tos))
                  promotions)))))

(deftest ^:parallel sql-is-parsed-as-duckdb-test
  (testing "Metabase parses this driver's native SQL in sqlglot's DuckDB dialect, and other drivers' as before"
    (let [driver->dialect @(requiring-resolve 'metabase.sql-tools.sqlglot.core/driver->dialect)]
      (is (= "duckdb" (driver->dialect :hardcharger)))
      (is (= "postgres" (driver->dialect :postgres)))
      (is (= "tsql" (driver->dialect :sqlserver)))
      (is (nil? (driver->dialect :h2)))))
  (mt/test-driver :hardcharger
    (testing "so the tables a DuckDB-only statement reads are found"
      (is (= #{{:table (mt/id :orders)} {:table (mt/id :products)}}
             (driver/native-query-deps
              :hardcharger
              (lib/native-query (mt/metadata-provider)
                                (str "SELECT o.* EXCLUDE (tax), list_value(p.price) AS prices "
                                     "FROM main.orders o JOIN main.products p ON p.id = o.product_id "
                                     "QUALIFY row_number() OVER (PARTITION BY o.user_id ORDER BY o.id) = 1"))))))))

(defn- uploads-schema-tables []
  (into #{} (map first)
        (native-rows (str "SELECT table_name FROM information_schema.tables"
                          " WHERE table_catalog = current_database() AND table_schema = 'hc_write_test'"))))

(deftest schemas-and-table-ddl-test
  (mt/test-driver :hardcharger
    (let [spec (driver/connection-spec :hardcharger (metadata-db))]
      (try
        (testing "a schema is made once, and seen"
          (is (false? (driver/schema-exists? :hardcharger (mt/id) "hc_write_test")))
          (driver/create-schema-if-needed! :hardcharger spec "hc_write_test")
          (driver/create-schema-if-needed! :hardcharger spec "hc_write_test")
          (is (true? (driver/schema-exists? :hardcharger (mt/id) "hc_write_test")))
          (is (contains? (driver/syncable-schemas :hardcharger (metadata-db)) "hc_write_test")))
        (testing "the schemas offered for uploads are this catalog's, without the system ones"
          (let [schemas (driver/syncable-schemas :hardcharger (metadata-db))]
            (is (contains? schemas "main"))
            (is (not-any? schemas ["information_schema" "pg_catalog"]))))
        (testing "create, insert every upload type, widen, add a column, rename, truncate, drop"
          (driver/create-table! :hardcharger (mt/id) :hc_write_test/t1
                                {:n         (driver/upload-type->database-type :hardcharger :metabase.upload/int)
                                 :flag      (driver/upload-type->database-type :hardcharger :metabase.upload/boolean)
                                 :at_local  (driver/upload-type->database-type :hardcharger :metabase.upload/datetime)
                                 :at_offset (driver/upload-type->database-type :hardcharger :metabase.upload/offset-datetime)
                                 :on_date   (driver/upload-type->database-type :hardcharger :metabase.upload/date)
                                 :label     (driver/upload-type->database-type :hardcharger :metabase.upload/text)})
          (driver/insert-into! :hardcharger (mt/id) :hc_write_test/t1
                               [:n :flag :at_local :at_offset :on_date :label]
                               [[1 true (LocalDateTime/of 2026 3 8 2 30) (OffsetDateTime/of 2026 3 8 1 30 0 0 (ZoneOffset/ofHours -5))
                                 (LocalDate/of 2026 3 8) "it's \"quoted\"\nand 日本語"]
                                [2 false nil nil nil nil]])
          (is (= [[1 true "2026-03-08 02:30:00" 1772951400 "2026-03-08" "it's \"quoted\"\nand 日本語"]
                  [2 false nil nil nil nil]]
                 (native-rows (str "SELECT n, flag, strftime(at_local, '%Y-%m-%d %H:%M:%S'), CAST(epoch(at_offset) AS BIGINT),"
                                   " CAST(on_date AS VARCHAR), label FROM hc_write_test.t1 ORDER BY n"))))
          (driver/alter-table-columns! :hardcharger (mt/id) :hc_write_test/t1
                                       {:n    (driver/upload-type->database-type :hardcharger :metabase.upload/float)
                                        :flag (driver/upload-type->database-type :hardcharger :metabase.upload/int)})
          (driver/add-columns! :hardcharger (mt/id) :hc_write_test/t1
                               {:extra  (driver/upload-type->database-type :hardcharger :metabase.upload/text)
                                :extra2 (driver/upload-type->database-type :hardcharger :metabase.upload/int)})
          (is (= [["n" "DOUBLE"] ["flag" "BIGINT"] ["at_local" "TIMESTAMP"] ["at_offset" "TIMESTAMP WITH TIME ZONE"]
                  ["on_date" "DATE"] ["label" "VARCHAR"] ["extra" "VARCHAR"] ["extra2" "BIGINT"]]
                 (native-rows (str "SELECT column_name, data_type FROM duckdb_columns()"
                                   " WHERE schema_name = 'hc_write_test' AND table_name = 't1' ORDER BY column_index"))))
          (is (= [[1.0 1] [2.0 0]] (native-rows "SELECT n, flag FROM hc_write_test.t1 ORDER BY n")))
          (testing "a swap of two tables is all or nothing"
            (driver/create-table! :hardcharger (mt/id) :hc_write_test/t2 {:n [:bigint]})
            (driver/rename-tables! :hardcharger (mt/id) {:hc_write_test/t1 :hc_write_test/t1_old
                                                          :hc_write_test/t2 :hc_write_test/t1})
            (is (= #{"t1" "t1_old"} (uploads-schema-tables)))
            (is (thrown? Exception
                         (driver/rename-tables! :hardcharger (mt/id) {:hc_write_test/t1     :hc_write_test/t3
                                                                       :hc_write_test/missing :hc_write_test/t4})))
            (is (= #{"t1" "t1_old"} (uploads-schema-tables)) "the first rename was rolled back with the second"))
          (driver/truncate! :hardcharger (mt/id) :hc_write_test/t1_old)
          (is (= [[0]] (native-rows "SELECT count(*) FROM hc_write_test.t1_old")))
          (driver/drop-table! :hardcharger (mt/id) :hc_write_test/t1_old)
          (driver/drop-table! :hardcharger (mt/id) :hc_write_test/t1)
          (is (= #{} (uploads-schema-tables))))
        (finally
          (execute-on-server! "DROP SCHEMA IF EXISTS hc_write_test CASCADE"))))))

