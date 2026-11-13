(ns metabase.driver.hardcharger
  "Metabase driver for Hardcharger, GizmoData's DuckDB 2.0 server.

  Hardcharger speaks the Chargewire protocol (SQL over HTTP, results as Arrow). This driver is built on the
  Hardcharger JDBC driver (`com.gizmodata/hardcharger-jdbc`, a thin layer over `chargewire-jdbc`), and generates
  DuckDB SQL."
  (:require
   [clojure.string :as str]
   [java-time.api :as t]
   [metabase.driver :as driver]
   [metabase.driver-api.core :as driver-api]
   [metabase.driver.settings :as driver.settings]
   [metabase.driver.sql-jdbc :as sql-jdbc]
   [metabase.driver.sql-jdbc.connection :as sql-jdbc.conn]
   [metabase.driver.sql-jdbc.execute :as sql-jdbc.execute]
   [metabase.driver.sql-jdbc.sync :as sql-jdbc.sync]
   [metabase.driver.sql.query-processor :as sql.qp]
   [metabase.driver.sql.query-processor.util :as sql.qp.u]
   [metabase.driver.sql.util :as sql.u]
   [metabase.driver.sync :as driver.s]
   [metabase.driver.util :as driver.u]
   [metabase.util :as u]
   [metabase.util.date-2 :as u.date]
   [metabase.util.honey-sql-2 :as h2x]
   [metabase.util.log :as log])
  (:import
   (com.gizmodata.chargewire.jdbc ChargewireConnection)
   (java.math BigDecimal)
   (java.net URLDecoder URLEncoder)
   (java.nio.charset StandardCharsets)
   (java.sql Connection PreparedStatement ResultSet ResultSetMetaData SQLException Types)
   (java.time LocalDate LocalDateTime LocalTime OffsetDateTime OffsetTime ZonedDateTime)
   (java.time.format DateTimeFormatter)
   (java.util Collections Map WeakHashMap)
   (java.util.concurrent Executors)))

(set! *warn-on-reflection* true)

;; In production the plugin manifest registers the driver before this namespace loads. Metabase's dev and test
;; harness loads driver namespaces directly and expects them to register themselves, as every in-tree driver does
;; (registering twice is harmless).
(driver/register! :hardcharger, :parent :sql-jdbc)

(defmethod driver/display-name :hardcharger [_driver] "Hardcharger")

;;; +----------------------------------------------------------------------------------------------------------------+
;;; |                                                    Features                                                    |
;;; +----------------------------------------------------------------------------------------------------------------+

;; The :sql and :sql-jdbc parents already answer true for the query builder's basics (joins, nested queries,
;; expressions, binning, regex, percentiles, standard deviation, window functions, native parameters). Only what
;; differs from them, or what is worth saying outright, is declared here. Everything declared false is something
;; Metabase then hides, instead of offering it and failing.
(doseq [[feature supported?]
        {;; sync: one query for all columns, one for all foreign keys
         :describe-fields                      true
         :describe-default-expr                true
         :describe-is-nullable                 true
         :describe-is-generated                false
         :metadata/key-constraints             true
         :metadata/table-existence-check       true
         ;; the session's TimeZone is set from Metabase's report time zone
         :set-timezone                         true
         :convert-timezone                     true
         :datetime-diff                        true
         :now                                  true
         :expression-literals                  true
         :expressions/date                     true
         :expressions/datetime                 true
         :expressions/float                    true
         :expressions/integer                  true
         :expressions/text                     true
         :expressions/today                    true
         :split-part                           true
         :uuid-type                            true
         :identifiers-with-spaces              true
         :schemas                              true
         ;; DuckDB's regular expressions are RE2: no lookaheads or lookbehinds
         :regex/lookaheads-and-lookbehinds     false
         ;; GROUP BY GROUPING SETS with GROUPING(a, b, ...)
         :native-pivot-tables                  true
         ;; One Metabase database is one catalog; other catalogs are reachable from SQL by their full name.
         :multi-level-schema                   false
         :connection/multiple-databases        false
         ;; JSON columns are synced and shown as text; unfolding their keys into columns is not implemented
         :nested-field-columns                 false
         :nested-fields                        false
         ;; DuckDB's ART indexes do not make a column worth flagging, and nothing reads them
         :index-info                           false
         :describe-indexes                     false
         :index/fetch                          false
         :index/standalone-create              false
         :index/inline-create                  false
         ;; Writing features. None is implemented: each needs DDL and error handling proven against Metabase's
         ;; own tests before it is offered.
         :actions                              false
         :actions/custom                       false
         :actions/data-editing                 false
         :persist-models                       false
         :uploads                              false
         :upload-with-auto-pk                  false
         :transforms/table                     false
         :transforms/python                    false
         :transforms/testing                   false
         ;; Access control (Enterprise features that need SET ROLE or per-user connections)
         :connection-impersonation             false
         :connection-impersonation-requires-role false
         :database-routing                     false
         :database-replication                 false
         :table-privileges                     false
         :metadata/table-writable-check        false}]
  (defmethod driver/database-supports? [:hardcharger feature] [_driver _feature _db] supported?))

;;; +----------------------------------------------------------------------------------------------------------------+
;;; |                                                   Connecting                                                   |
;;; +----------------------------------------------------------------------------------------------------------------+

(def ^:private jdbc-driver-class "com.gizmodata.hardcharger.jdbc.HardchargerDriver")

(def ^:private allowed-additional-options
  "The JDBC properties `additional-options` may set, by lower-cased name, with the spelling handed to the driver.

  This is an allowlist on purpose. The JDBC driver has properties that name a program to run (`oauthBrowser`), files
  on Metabase's server (`tlsRootCerts`, `trustStore`, `clientCertificate`, `clientKey`), and credentials (`user`,
  `password`, `token`, `subjectToken`); none of those may come from free text. Credentials and certificates have
  fields of their own, stored as secrets."
  (into {}
        (map (juxt u/lower-case-en identity))
        ["async" "pollWait" "retainResults" "ipcCompression" "resultBufferSize" "resultPartitions" "resultOrder"
         "connectTimeout" "networkTimeout" "resumeTimeout" "basePath" "useSystemTrustStore"]))

(defn- non-blank [s]
  (when (and (string? s) (not (str/blank? s)))
    (str/trim s)))

(defn- parse-additional-options
  "Parse `key=value` pairs separated by `&` or `;` into a map of JDBC property name to value. Throws on anything that
  is not an allowed option. The message names the key, never the value."
  [s]
  (into {}
        (for [pair  (some-> (non-blank s) (str/replace #"^[?&;]+" "") (str/split #"[&;]"))
              :when (non-blank pair)
              :let  [[k v] (str/split pair #"=" 2)
                     k     (str/trim (or k ""))
                     prop  (get allowed-additional-options (u/lower-case-en k))]]
          (cond
            (nil? prop)
            (throw (ex-info (format "Unsupported additional JDBC option \"%s\". Allowed options: %s."
                                    k (str/join ", " (sort (vals allowed-additional-options))))
                            {:type driver-api/qp.error-type.invalid-parameter, :option k}))

            (nil? (non-blank v))
            (throw (ex-info (format "The additional JDBC option \"%s\" needs a value (%s=...)." prop prop)
                            {:type driver-api/qp.error-type.invalid-parameter, :option k}))

            :else
            [prop (URLDecoder/decode (str/trim v) StandardCharsets/UTF_8)]))))

(defn- secret-string
  "The value of a secret-typed connection property. The admin form stores it as `<prop>-value` or `<prop>-id`; a
  database created through the API may carry it as a plain key."
  [details prop]
  (or (non-blank (driver-api/secret-value-as-string :hardcharger details prop))
      (non-blank (get details (keyword prop)))))

(defn- secret-file
  "The path of a file holding a secret-typed connection property, for the JDBC driver's properties that take PEM
  file paths. Metabase writes an uploaded value to a private temporary file, and gives the same file again for the
  same details, so the connection spec stays stable."
  [details prop]
  (some-> (driver-api/secret-value-as-file! :hardcharger details prop) str non-blank))

(defn- host-for-url [host]
  (let [host (or (non-blank host) "localhost")]
    ;; an IPv6 literal goes in brackets
    (if (and (str/includes? host ":") (not (str/starts-with? host "[")))
      (str "[" host "]")
      host)))

(defn- port-for-url [port]
  (when-let [port (cond
                    (integer? port) port
                    (string? port)  (some-> (non-blank port) parse-long))]
    (when-not (<= 1 port 65535)
      (throw (ex-info (format "Port %d is out of range (1 to 65535)." port)
                      {:type driver-api/qp.error-type.invalid-parameter})))
    port))

(defn- credentials
  "The JDBC properties that sign in, from the form's sign-in mode: the client certificate, an access token, or a user
  name and password. Only the chosen mode's credentials are sent."
  [{:keys [use-token mtls-sign-in user password], :as details}]
  (cond
    mtls-sign-in
    {"authType" "client_certificate"}

    use-token
    (if-let [token (secret-string details "token")]
      {"token" token}
      (throw (ex-info "Enter an access token, or turn off \"Sign in with an access token\"."
                      {:type driver-api/qp.error-type.invalid-parameter})))

    :else
    (cond-> {}
      (non-blank user)     (assoc "user" (non-blank user))
      ;; a password is sent as typed: it may begin or end with a space
      (seq (str password)) (assoc "password" (str password)))))

(defmethod sql-jdbc.conn/connection-details->spec :hardcharger
  [_driver {:keys [host port catalog tls tls-skip-verify mtls-sign-in additional-options], :as details}]
  (let [tls?        (if (nil? tls) true (boolean tls))
        ca-file     (secret-file details "tls-ca-certificate")
        cert-file   (secret-file details "mtls-certificate")
        key-file    (secret-file details "mtls-key")
        catalog     (non-blank catalog)]
    (when (and (or cert-file key-file) (not (and cert-file key-file)))
      (throw (ex-info "Mutual TLS needs both the client certificate and the client key."
                      {:type driver-api/qp.error-type.invalid-parameter})))
    (when (and mtls-sign-in (not (and tls? cert-file key-file)))
      (throw (ex-info "Signing in with the client certificate needs TLS, the client certificate and the client key."
                      {:type driver-api/qp.error-type.invalid-parameter})))
    ;; Only the host, the port and the catalog are part of the URL. Everything else, credentials included, travels
    ;; as JDBC properties, so no value needs URL escaping and none can smuggle in another option. The map holds
    ;; only strings: Metabase hashes it to decide whether its connection pool is stale.
    (merge
     (parse-additional-options additional-options)
     {:classname   jdbc-driver-class
      :subprotocol "hardcharger"
      :subname     (str "//" (host-for-url host)
                        (when-let [port (port-for-url port)] (str ":" port))
                        (when catalog (str "/" (str/replace (URLEncoder/encode ^String catalog StandardCharsets/UTF_8) "+" "%20"))))
      "useEncryption" (str tls?)}
     (when (and tls? tls-skip-verify) {"disableCertificateVerification" "true"})
     (when (and tls? ca-file)         {"tlsRootCerts" ca-file})
     (when (and tls? cert-file)       {"clientCertificate" cert-file, "clientKey" key-file})
     (credentials details))))

;; Parameters a user may never set, in the additional options or as a detail key of their own making. The allowlist
;; above already refuses them in the additional options; this tells Metabase's own check too.
(defmethod driver/disallowed-connection-parameters :hardcharger
  [driver]
  (into ((get-method driver/disallowed-connection-parameters :sql-jdbc) driver)
        ["oauthBrowser" "oauthTimeout" "authType" "subjectToken" "trustStore" "tlsRootCerts" "clientCertificate"
         "clientKey" "disableCertificateVerification"]))

;; None of the JDBC driver's properties names another host to connect to. (A server may hand a client result
;; partitions held by other servers of its cluster; that is the server's doing, not a connection property.)
(defmethod driver/host-carrying-parameters :hardcharger [_driver] [])

(defmethod driver/non-host-parameters :hardcharger
  [_driver]
  ;; a path on the connection's own host
  ["basePath"])

(defn- exception-chain [^Throwable e]
  (take-while some? (iterate #(.getCause ^Throwable %) e)))

(defn- sql-state [^Throwable e]
  (some #(when (instance? SQLException %) (.getSQLState ^SQLException %)) (exception-chain e)))

(defmethod driver/humanize-connection-error-message :hardcharger
  [_driver messages]
  (let [message (str (first messages))]
    (condp re-find message
      #"(?i)user name or password is not correct"
      :username-or-password-incorrect

      #"(?i)^No credentials"
      "Enter a user name and password, or sign in with an access token."

      #"(?i)valid access token is required"
      "The server refused the access token: it is wrong, expired or revoked."

      #"(?i)UnknownHostException|UnresolvedAddressException"
      :invalid-hostname

      #"(?i)plaintext connection\?"
      "The server at this host and port does not use TLS. Turn off \"Use a secure connection (TLS)\", or check the port."

      #"(?i)^TLS failure|PKIX path|certificate_unknown|unable to find valid certification path"
      (str "The server's TLS certificate could not be verified. Add its certificate authority under "
           "\"Server CA certificate (PEM)\" in the advanced options. (" message ")")

      #"(?i)^Cannot connect to|ConnectException|Connection refused|timed out"
      :cannot-connect-check-host-and-port

      #"(?i)No catalog \+ schema named"
      "The server has no catalog of that name. Check \"Catalog\", or leave it empty for the server's default."

      message)))

;;; ---------------------------------------- Connections and their session ----------------------------------------

(def ^:private ^Map sessions-with-a-time-zone
  "The server sessions whose TimeZone this driver has set, as JDBC connection -> session id. Weak keys: an entry goes
  when its connection does.

  It answers one question: must a connection that is asked for no time zone be put back into the server's own? Only
  when it still carries one from an earlier query (the report time zone was removed, or the pool is shared with a
  query that had one). The session id is kept because the JDBC driver opens a new session when the server no longer
  has the old one, and a new session starts in the server's zone."
  (Collections/synchronizedMap (WeakHashMap.)))

(defn- physical-connection ^ChargewireConnection [^Connection conn]
  (try
    (.unwrap conn ChargewireConnection)
    (catch Throwable _ nil)))

(defn- execute! [^Connection conn ^String sql]
  (with-open [stmt (.createStatement conn)]
    (.execute stmt sql)))

(defn- set-session-time-zone!
  "Make the session's TimeZone `zone-id`, or the server's own when it is nil.

  With a zone, the statement is sent on every checkout, as Metabase's other JDBC drivers do: a native query may have
  changed the session's zone since, and a wrong zone silently shifts every timestamp with a time zone."
  [^Connection conn zone-id]
  (let [physical (physical-connection conn)]
    (if zone-id
      (do
        ;; a zone id is letters, digits and / _ + - : only; the quote is doubled anyway
        (execute! conn (format "SET TimeZone = '%s'" (str/replace zone-id "'" "''")))
        (when physical
          (.put sessions-with-a-time-zone physical (str (.getSessionId physical)))))
      (when (and physical
                 (= (.get sessions-with-a-time-zone physical) (str (.getSessionId physical))))
        (execute! conn "RESET TimeZone")
        (.remove sessions-with-a-time-zone physical)))))

;; Used by Metabase's test data loader, and by anything else that sets a zone the old way. The driver's own
;; connections go through set-session-time-zone! above.
(defmethod sql-jdbc.execute/set-timezone-sql :hardcharger
  [_driver]
  "SET TimeZone = %s")

(defonce ^:private network-timeout-executor
  (delay (Executors/newCachedThreadPool)))

(defn- set-connection-options!
  "What Metabase sets on every JDBC connection it borrows (see `set-default-connection-options!`), without its
  transaction isolation probe (DuckDB has one isolation level) and its own time zone statement."
  [^Connection conn {:keys [write?]}]
  ;; Read-only is a hint to the JDBC driver. Hardcharger decides who may write by user and role, or for the whole
  ;; server with --read-only; it has no read-only session.
  (try
    (.setReadOnly conn (not write?))
    (catch Throwable e
      (log/debugf "Error setting connection readOnly: %s" (ex-message e))))
  (when-not write?
    (try
      (.setAutoCommit conn true)
      (catch Throwable e
        (log/debugf "Error enabling connection autoCommit: %s" (ex-message e)))))
  (try
    (.setNetworkTimeout conn @network-timeout-executor driver.settings/*network-timeout-ms*)
    (catch Throwable e
      (log/debugf "Error setting network timeout for connection: %s" (ex-message e))))
  (try
    (.setHoldability conn ResultSet/CLOSE_CURSORS_AT_COMMIT)
    (catch Throwable e
      (log/debugf "Error setting default holdability for connection: %s" (ex-message e)))))

(defmethod sql-jdbc.execute/do-with-connection-with-options :hardcharger
  [driver db-or-id-or-spec {:keys [^String session-timezone], :as options} f]
  (sql-jdbc.execute/do-with-resolved-connection
   driver
   db-or-id-or-spec
   options
   (fn [^Connection conn]
     (when-not (sql-jdbc.execute/recursive-connection?)
       (set-session-time-zone! conn session-timezone)
       (set-connection-options! conn options))
     (f conn))))

(defmethod driver/db-default-timezone :hardcharger
  [driver database]
  ;; with no session time zone asked for, the connection is in the server's own zone
  (sql-jdbc.execute/do-with-connection-with-options
   driver database nil
   (fn [^Connection conn]
     (with-open [stmt (.createStatement conn)
                 rs   (.executeQuery stmt "SELECT current_setting('TimeZone')")]
       (when (.next rs)
         (.getString rs 1))))))

(defmethod driver/db-start-of-week :hardcharger [_driver] :monday)

;; A cancelled statement and one that ran into its timeout both end with SQLSTATE 57014.
(defmethod sql-jdbc/impl-query-canceled? :hardcharger
  [_driver ^SQLException e]
  (= (.getSQLState e) "57014"))

(defmethod sql-jdbc/impl-table-known-to-not-exist? :hardcharger
  [_driver ^SQLException e]
  (= (.getSQLState e) "42P01"))

;;; +----------------------------------------------------------------------------------------------------------------+
;;; |                                                      Sync                                                      |
;;; +----------------------------------------------------------------------------------------------------------------+

(def ^:private database-type->base-type
  "DuckDB type name to Metabase base type; the first match wins. A column's type arrives in full from the catalog
  (`DECIMAL(18,3)`, `STRUCT(a INTEGER, b VARCHAR)`, `ENUM('a', 'b')`, `INTEGER[]`), and by JDBC type name from a
  result set (`REAL`, `VARBINARY`, `STRUCT`), so nested types are matched as a whole before the scalar names they
  contain."
  (sql-jdbc.sync/pattern-based-database-type->base-type
   [[#"(?i)\[\d*\]$"                                             :type/Array]
    [#"(?i)^(?:STRUCT|MAP)\b"                                    :type/Dictionary]
    ;; a UNION's value is whichever member it holds; a VARIANT arrives in Arrow's binary variant encoding
    [#"(?i)^(?:UNION|VARIANT)\b"                                 :type/*]
    [#"(?i)^ENUM\b"                                              :type/Text]
    [#"(?i)^JSON$"                                               :type/JSON]
    [#"(?i)^BOOL(?:EAN)?$|^LOGICAL$"                             :type/Boolean]
    [#"(?i)^(?:TINYINT|SMALLINT|INTEGER|INT|INT1|INT2|INT4|SHORT|SIGNED|UTINYINT|USMALLINT)$" :type/Integer]
    [#"(?i)^(?:BIGINT|INT8|LONG|UINTEGER|UBIGINT|HUGEINT|UHUGEINT)$" :type/BigInteger]
    ;; a decimal without a fraction holds whole numbers (and is how HUGEINT and SUM(integer) arrive)
    [#"(?i)^(?:DECIMAL|NUMERIC)\(\d+,\s*0\)$"                    :type/BigInteger]
    [#"(?i)^(?:DECIMAL|NUMERIC)\b"                               :type/Decimal]
    [#"(?i)^(?:FLOAT|FLOAT4|FLOAT8|REAL|DOUBLE)$"                :type/Float]
    [#"(?i)^(?:VARCHAR|CHAR|BPCHAR|TEXT|STRING)\b"               :type/Text]
    [#"(?i)^UUID$"                                               :type/UUID]
    [#"(?i)^DATE$"                                               :type/Date]
    ;; TIME WITH TIME ZONE is read as its local time: Arrow has no time with an offset, so the offset is not sent
    [#"(?i)^(?:TIME|TIME_NS|TIMETZ|TIME WITH TIME ZONE)$"        :type/Time]
    [#"(?i)^(?:TIMESTAMPTZ|TIMESTAMP WITH TIME ZONE)$"           :type/DateTimeWithLocalTZ]
    [#"(?i)^(?:TIMESTAMP|DATETIME|TIMESTAMP_S|TIMESTAMP_MS|TIMESTAMP_US|TIMESTAMP_NS)$" :type/DateTime]
    ;; INTERVAL, BLOB, BIT, BIGNUM (binary on the wire), GEOMETRY and anything new
    [#".*"                                                       :type/*]]))

(defmethod sql-jdbc.sync/database-type->base-type :hardcharger
  [_driver database-type]
  (database-type->base-type database-type))

(defmethod sql-jdbc.sync/excluded-schemas :hardcharger
  [_driver]
  #{"information_schema" "pg_catalog"})

(defn- in-list
  "`AND <column> IN (?, ...)` and its parameters, or nothing when there are no values to restrict to."
  [column values]
  (if (seq values)
    [(format " AND %s IN (%s)" column (str/join ", " (repeat (count values) "?"))) (vec values)]
    ["" []]))

(defn- schema-filter-patterns [driver database]
  (when-let [prop (driver.u/find-schema-filters-prop driver)]
    (driver.s/db-details->schema-filter-patterns (:name prop) database)))

(defn- describe-database-sql
  "Tables and views of the session's catalog, with their comments. The catalog is the one the connection opened:
  the connection's Catalog setting, or the server's default."
  [driver]
  (let [[excluded-sql excluded] (in-list "table_schema NOT" (sort (sql-jdbc.sync/excluded-schemas driver)))]
    (into [(str "SELECT table_schema AS \"schema\", table_name AS \"name\", table_comment AS \"description\""
                " FROM information_schema.tables"
                " WHERE table_catalog = current_database()"
                " AND table_type IN ('BASE TABLE', 'VIEW')"
                excluded-sql
                " ORDER BY table_schema, table_name")]
          excluded)))

(defmethod driver/describe-database* :hardcharger
  [driver database]
  (let [[inclusion-patterns exclusion-patterns] (schema-filter-patterns driver database)]
    {:tables
     (into #{}
           (comp (remove (fn [{:keys [schema]}] (re-find #"^metabase_cache" (str schema))))
                 (filter (fn [{:keys [schema]}]
                           (driver.s/include-schema? inclusion-patterns exclusion-patterns schema)))
                 (map (fn [{:keys [schema name description]}]
                        {:schema      schema
                         :name        name
                         :description (non-blank description)})))
           (sql-jdbc.execute/reducible-query database (describe-database-sql driver)))}))

(defmethod sql-jdbc.sync/describe-fields-sql :hardcharger
  [driver & {:keys [schema-names table-names]}]
  ;; duckdb_columns() gives each column's full type (DECIMAL(18,3), ENUM(...), STRUCT(...)), for tables and views;
  ;; JDBC metadata gives the bare type name. The names in the SELECT list are the keys Metabase reads.
  (let [[excluded-sql excluded] (in-list "c.schema_name NOT" (sort (sql-jdbc.sync/excluded-schemas driver)))
        [schemas-sql schemas]   (in-list "c.schema_name" schema-names)
        [tables-sql tables]     (in-list "c.table_name" table-names)]
    (into [(str "SELECT c.column_name AS \"name\","
                " c.data_type AS \"database-type\","
                " c.column_index - 1 AS \"database-position\","
                " c.schema_name AS \"table-schema\","
                " c.table_name AS \"table-name\","
                " pk.column_name IS NOT NULL AS \"pk?\","
                " c.comment AS \"field-comment\","
                " c.column_default AS \"database-default\","
                " (NOT c.is_nullable AND c.column_default IS NULL) AS \"database-required\","
                " coalesce(c.column_default LIKE 'nextval(%', false) AS \"database-is-auto-increment\","
                " c.is_nullable AS \"database-is-nullable\""
                " FROM duckdb_columns() c"
                " LEFT JOIN (SELECT schema_name, table_name, unnest(constraint_column_names) AS column_name"
                "            FROM duckdb_constraints()"
                "            WHERE database_name = current_database() AND constraint_type = 'PRIMARY KEY') pk"
                "   ON pk.schema_name = c.schema_name AND pk.table_name = c.table_name"
                "  AND pk.column_name = c.column_name"
                " WHERE c.database_name = current_database()"
                excluded-sql schemas-sql tables-sql
                " ORDER BY c.schema_name, c.table_name, c.column_index")]
          cat
          [excluded schemas tables])))

(defmethod sql-jdbc.sync/describe-fks-sql :hardcharger
  [driver & {:keys [schema-names table-names]}]
  (let [[excluded-sql excluded] (in-list "fk.table_schema NOT" (sort (sql-jdbc.sync/excluded-schemas driver)))
        [schemas-sql schemas]   (in-list "fk.table_schema" schema-names)
        [tables-sql tables]     (in-list "fk.table_name" table-names)]
    (into [(str "SELECT fk.table_schema AS \"fk-table-schema\","
                " fk.table_name AS \"fk-table-name\","
                " fk.column_name AS \"fk-column-name\","
                " pk.table_schema AS \"pk-table-schema\","
                " pk.table_name AS \"pk-table-name\","
                " pk.column_name AS \"pk-column-name\""
                " FROM information_schema.referential_constraints rc"
                " JOIN information_schema.key_column_usage fk"
                "   ON fk.constraint_catalog = rc.constraint_catalog"
                "  AND fk.constraint_schema = rc.constraint_schema"
                "  AND fk.constraint_name = rc.constraint_name"
                " JOIN information_schema.key_column_usage pk"
                "   ON pk.constraint_catalog = rc.unique_constraint_catalog"
                "  AND pk.constraint_schema = rc.unique_constraint_schema"
                "  AND pk.constraint_name = rc.unique_constraint_name"
                "  AND pk.ordinal_position = fk.position_in_unique_constraint"
                " WHERE rc.constraint_catalog = current_database()"
                excluded-sql schemas-sql tables-sql
                " ORDER BY fk.table_schema, fk.table_name, fk.ordinal_position")]
          cat
          [excluded schemas tables])))

(defmethod driver/table-exists? :hardcharger
  [_driver database {:keys [schema name]}]
  (boolean
   (seq (into []
              (take 1)
              (sql-jdbc.execute/reducible-query
               database
               (if schema
                 [(str "SELECT 1 FROM information_schema.tables"
                       " WHERE table_catalog = current_database() AND table_schema = ? AND table_name = ?")
                  schema name]
                 [(str "SELECT 1 FROM information_schema.tables"
                       " WHERE table_catalog = current_database() AND table_schema = current_schema()"
                       " AND table_name = ?")
                  name]))))))

;;; +----------------------------------------------------------------------------------------------------------------+
;;; |                                            Reading and binding values                                          |
;;; +----------------------------------------------------------------------------------------------------------------+

;; The JDBC driver converts DATE, TIME and both TIMESTAMPs to java.time itself, which the :sql-jdbc defaults ask
;; for. What follows is the rest.

(defn- whole-number
  "A decimal without a fraction as a Long when it fits, else a BigInteger: HUGEINT, UBIGINT and SUM over an integer
  column arrive as DECIMAL(38,0) or NUMERIC(20), and are whole numbers to Metabase."
  [^BigDecimal d]
  (when d
    (let [i (.toBigInteger d)]
      (if (<= (.bitLength i) 63)
        (.longValue i)
        i))))

(defn- decimal-thunk [^ResultSet rs ^ResultSetMetaData rsmeta ^Integer i]
  (if (zero? (.getScale rsmeta i))
    (fn [] (whole-number (.getBigDecimal rs i)))
    (fn [] (.getBigDecimal rs i))))

(defmethod sql-jdbc.execute/read-column-thunk [:hardcharger Types/DECIMAL]
  [_driver rs rsmeta i]
  (decimal-thunk rs rsmeta i))

(defmethod sql-jdbc.execute/read-column-thunk [:hardcharger Types/NUMERIC]
  [_driver rs rsmeta i]
  (decimal-thunk rs rsmeta i))

;; Lists, structs, maps and unions: the JDBC driver's text for them (`[1, 2, 3]`, `{"a": 1, "b": "x"}`), which
;; reads the same in a table cell, a CSV and an API response. Its objects (lists and maps of JDBC values) do not.
(doseq [jdbc-type [Types/ARRAY Types/STRUCT Types/JAVA_OBJECT]]
  (defmethod sql-jdbc.execute/read-column-thunk [:hardcharger jdbc-type]
    [_driver ^ResultSet rs _rsmeta ^Integer i]
    (fn [] (.getString rs i))))

;; OTHER is a UUID (kept as one) or an INTERVAL (as ISO-8601 text, P1Y2M3DT4H).
(defmethod sql-jdbc.execute/read-column-thunk [:hardcharger Types/OTHER]
  [_driver ^ResultSet rs _rsmeta ^Integer i]
  (fn []
    (let [obj (.getObject rs i)]
      (if (or (nil? obj) (uuid? obj))
        obj
        (str obj)))))

;; The JDBC driver has no parameter of a time with an offset (nor has Arrow): send the local time.
(defmethod sql-jdbc.execute/set-parameter [:hardcharger OffsetTime]
  [_driver ^PreparedStatement ps ^Integer i ^OffsetTime t]
  (.setObject ps i (.toLocalTime t)))

;;; +----------------------------------------------------------------------------------------------------------------+
;;; |                                                 Generating SQL                                                 |
;;; +----------------------------------------------------------------------------------------------------------------+

;;; ------------------------------------------------- Literals -------------------------------------------------

;; DuckDB takes an offset only directly after the time ('... 10:00:00+02:00'); with a space before it, as the :sql
;; default writes it, the offset is read as a time zone name and refused.
(def ^:private ^DateTimeFormatter timestamp-formatter (DateTimeFormatter/ofPattern "yyyy-MM-dd HH:mm:ss.SSSSSS"))
(def ^:private ^DateTimeFormatter time-formatter      (DateTimeFormatter/ofPattern "HH:mm:ss.SSSSSS"))
(def ^:private ^DateTimeFormatter offset-formatter    (DateTimeFormatter/ofPattern "xxx"))

(defmethod sql.qp/inline-value [:hardcharger LocalDate]
  [_driver ^LocalDate t]
  (format "DATE '%s'" (.format t DateTimeFormatter/ISO_LOCAL_DATE)))

(defmethod sql.qp/inline-value [:hardcharger LocalTime]
  [_driver ^LocalTime t]
  (format "TIME '%s'" (.format t time-formatter)))

(defmethod sql.qp/inline-value [:hardcharger OffsetTime]
  [_driver ^OffsetTime t]
  (format "TIMETZ '%s%s'" (.format t time-formatter) (.format t offset-formatter)))

(defmethod sql.qp/inline-value [:hardcharger LocalDateTime]
  [_driver ^LocalDateTime t]
  (format "TIMESTAMP '%s'" (.format t timestamp-formatter)))

(defmethod sql.qp/inline-value [:hardcharger OffsetDateTime]
  [_driver ^OffsetDateTime t]
  (format "TIMESTAMPTZ '%s%s'" (.format t timestamp-formatter) (.format t offset-formatter)))

(defmethod sql.qp/inline-value [:hardcharger ZonedDateTime]
  [driver t]
  (sql.qp/inline-value driver (t/offset-date-time t)))

;;; -------------------------------------------- Dates and times --------------------------------------------

(defn- db-type
  "The lower-cased database type an expression is tagged with, if any."
  [expr]
  (some-> (h2x/database-type expr) u/lower-case-en))

(defn- date? [expr]
  (h2x/database-or-effective-type-isa? expr "date" :type/Date))

(defn- time? [expr]
  (or (contains? #{"time" "time_ns" "timetz" "time with time zone"} (db-type expr))
      (h2x/database-or-effective-type-isa? expr "time" :type/Time)))

(defn- timestamptz? [expr]
  (contains? #{"timestamptz" "timestamp with time zone"} (db-type expr)))

(defn- date-trunc
  "`date_trunc(unit, expr)` that keeps `expr`'s type. DuckDB's date_trunc answers a TIMESTAMP for a DATE, and does not
  take a TIME at all."
  [unit expr]
  (cond
    (time? expr)
    (-> (h2x/cast "time" [:date_trunc (h2x/literal unit) (h2x/+ [:raw "DATE '1970-01-01'"] expr)])
        (h2x/with-database-type-info "time"))

    (date? expr)
    (h2x/cast "date" [:date_trunc (h2x/literal unit) expr])

    :else
    (cond-> [:date_trunc (h2x/literal unit) expr]
      (h2x/type-info expr) (h2x/with-type-info (h2x/type-info expr)))))

;; Truncating and extracting a TIMESTAMP WITH TIME ZONE both happen in the session's TimeZone, which is Metabase's
;; report time zone.
(defmethod sql.qp/date [:hardcharger :default]          [_driver _unit expr] expr)
(defmethod sql.qp/date [:hardcharger :second-of-minute] [_driver _unit expr] [:second expr])
(defmethod sql.qp/date [:hardcharger :minute]           [_driver _unit expr] (date-trunc :minute expr))
(defmethod sql.qp/date [:hardcharger :minute-of-hour]   [_driver _unit expr] [:minute expr])
(defmethod sql.qp/date [:hardcharger :hour]             [_driver _unit expr] (date-trunc :hour expr))
(defmethod sql.qp/date [:hardcharger :hour-of-day]      [_driver _unit expr] [:hour expr])
(defmethod sql.qp/date [:hardcharger :day]              [_driver _unit expr] (date-trunc :day expr))
(defmethod sql.qp/date [:hardcharger :day-of-month]     [_driver _unit expr] [:day expr])
(defmethod sql.qp/date [:hardcharger :day-of-year]      [_driver _unit expr] [:dayofyear expr])
(defmethod sql.qp/date [:hardcharger :month]            [_driver _unit expr] (date-trunc :month expr))
(defmethod sql.qp/date [:hardcharger :month-of-year]    [_driver _unit expr] [:month expr])
(defmethod sql.qp/date [:hardcharger :quarter]          [_driver _unit expr] (date-trunc :quarter expr))
(defmethod sql.qp/date [:hardcharger :quarter-of-year]  [_driver _unit expr] [:quarter expr])
(defmethod sql.qp/date [:hardcharger :year]             [_driver _unit expr] (date-trunc :year expr))
(defmethod sql.qp/date [:hardcharger :year-of-era]      [_driver _unit expr] [:year expr])
;; ISO 8601 week number
(defmethod sql.qp/date [:hardcharger :week-of-year-iso] [_driver _unit expr] [:week expr])

;; isodow: Monday is 1 and Sunday 7, which is this driver's db-start-of-week; Metabase shifts it to its own
;; start-of-week setting.
(defmethod sql.qp/date [:hardcharger :day-of-week]
  [driver _unit expr]
  (sql.qp/adjust-day-of-week driver [:isodow expr]))

;; date_trunc('week') starts weeks on Monday; Metabase shifts to its own start-of-week setting around it.
(defmethod sql.qp/date [:hardcharger :week]
  [driver _unit expr]
  (sql.qp/adjust-start-of-week driver (partial date-trunc :week) expr))

(defn- interval
  "`INTERVAL 'amount' unit` as HoneySQL. The amount is a number formatted here and the unit one of a fixed set, so
  nothing of a query's values reaches the SQL text."
  [amount unit]
  (let [unit   (get {:millisecond "MILLISECOND", :second "SECOND", :minute "MINUTE", :hour "HOUR", :day "DAY",
                     :week "WEEK", :month "MONTH", :quarter "QUARTER", :year "YEAR"}
                    unit)
        amount (if (and (number? amount) (== amount (long amount))) (long amount) amount)]
    (when-not (and unit (number? amount))
      (throw (ex-info "Invalid interval" {:amount amount, :unit unit})))
    [:raw (format "INTERVAL '%s' %s" amount unit)]))

(defmethod sql.qp/add-interval-honeysql-form :hardcharger
  [driver hsql-form amount unit]
  (cond
    ;; DuckDB's interval amounts are whole numbers: send fractional seconds as milliseconds
    (and (= unit :second) (number? amount) (not (== amount (long amount))))
    (recur driver hsql-form (Math/round (* 1000.0 (double amount))) :millisecond)

    ;; DATE + INTERVAL is a TIMESTAMP in DuckDB: a date moved by whole days stays a date
    (and (date? hsql-form) (contains? #{:day :week :month :quarter :year} unit))
    (h2x/cast "date" (h2x/+ hsql-form (interval amount unit)))

    :else
    (cond-> (h2x/+ hsql-form (interval amount unit))
      (h2x/type-info hsql-form) (h2x/with-type-info (h2x/type-info hsql-form)))))

(defmethod sql.qp/current-datetime-honeysql-form :hardcharger
  [_driver]
  (h2x/with-database-type-info [:now] "timestamp with time zone"))

;; to_timestamp answers a TIMESTAMP WITH TIME ZONE
(defmethod sql.qp/unix-timestamp->honeysql [:hardcharger :seconds]
  [_driver _unit expr]
  (h2x/with-database-type-info [:to_timestamp (h2x/cast "double" expr)] "timestamp with time zone"))

(defmethod sql.qp/cast-temporal-string [:hardcharger :Coercion/YYYYMMDDHHMMSSString->Temporal]
  [_driver _coercion-strategy expr]
  (h2x/with-database-type-info [:strptime expr (h2x/literal "%Y%m%d%H%M%S")] "timestamp"))

(defmethod sql.qp/cast-temporal-byte [:hardcharger :Coercion/YYYYMMDDHHMMSSBytes->Temporal]
  [driver _coercion-strategy expr]
  (sql.qp/cast-temporal-string driver :Coercion/YYYYMMDDHHMMSSString->Temporal [:decode expr]))

(defmethod sql.qp/cast-temporal-byte [:hardcharger :Coercion/ISO8601Bytes->Temporal]
  [driver _coercion-strategy expr]
  (sql.qp/cast-temporal-string driver :Coercion/ISO8601->DateTime [:decode expr]))

;; As in PostgreSQL: timezone(zone, TIMESTAMP) reads a wall-clock time in that zone and answers a TIMESTAMP WITH TIME
;; ZONE; timezone(zone, TIMESTAMP WITH TIME ZONE) answers the wall-clock time there.
(defmethod sql.qp/->honeysql [:hardcharger :convert-timezone]
  [driver [_ _opts arg target-timezone source-timezone]]
  (let [expr         (sql.qp/->honeysql driver (cond-> arg (string? arg) u.date/parse))
        timestamptz? (or (timestamptz? expr)
                         (sql.qp.u/field-with-tz? arg))
        _            (sql.u/validate-convert-timezone-args timestamptz? target-timezone source-timezone)
        expr         (cond-> expr
                       (or (instance? LocalDate expr) (date? expr)) h2x/->timestamp)]
    (h2x/with-database-type-info
     [:timezone target-timezone (if timestamptz?
                                  expr
                                  [:timezone source-timezone expr])]
     "timestamp")))

;; Whole units between two moments, as Metabase counts them: years, quarters, months, weeks and days between the two
;; calendar dates, and hours, minutes and seconds of time passed.
(defn- ->date [expr] (h2x/cast "date" expr))

(defmethod sql.qp/datetime-diff [:hardcharger :year]
  [_driver _unit x y]
  [:date_sub (h2x/literal :year) (->date x) (->date y)])

(defmethod sql.qp/datetime-diff [:hardcharger :quarter]
  [_driver _unit x y]
  [:date_sub (h2x/literal :quarter) (->date x) (->date y)])

(defmethod sql.qp/datetime-diff [:hardcharger :month]
  [_driver _unit x y]
  [:date_sub (h2x/literal :month) (->date x) (->date y)])

(defmethod sql.qp/datetime-diff [:hardcharger :week]
  [_driver _unit x y]
  [:date_sub (h2x/literal :week) (->date x) (->date y)])

(defmethod sql.qp/datetime-diff [:hardcharger :day]
  [_driver _unit x y]
  [:date_diff (h2x/literal :day) (->date x) (->date y)])

(doseq [unit [:hour :minute :second]]
  (defmethod sql.qp/datetime-diff [:hardcharger unit]
    [_driver unit x y]
    [:date_sub (h2x/literal unit) x y]))

;;; ------------------------------------------ Functions and casts ------------------------------------------

(defmethod sql.qp/float-dbtype :hardcharger [_driver] "DOUBLE")

(defmethod sql.qp/integer-dbtype :hardcharger [_driver] "BIGINT")

(defmethod sql.qp/->honeysql [:hardcharger :median]
  [driver [_ _opts arg]]
  [:median (sql.qp/->honeysql driver arg)])

(defmethod sql.qp/->honeysql [:hardcharger :percentile]
  [driver [_ _opts arg p]]
  [:quantile_cont (sql.qp/->honeysql driver arg) (sql.qp/->honeysql driver p)])

;; regexp_extract answers '' where nothing matches; Metabase's regexextract answers no value
(defmethod sql.qp/->honeysql [:hardcharger :regex-match-first]
  [driver [_ _opts arg pattern]]
  [:nullif [:regexp_extract (sql.qp/->honeysql driver arg) (sql.qp/->honeysql driver pattern)] (h2x/literal "")])

(defmethod sql.qp/->honeysql [:hardcharger :split-part]
  [driver [_ _opts text divider position]]
  (let [position (sql.qp/->honeysql driver position)]
    [:case
     [:< position 1] (h2x/literal "")
     :else           [:split_part (sql.qp/->honeysql driver text) (sql.qp/->honeysql driver divider) position]]))

(defmethod sql.qp/->honeysql [:hardcharger :text]
  [driver [_ _opts value]]
  (h2x/maybe-cast "VARCHAR" (sql.qp/->honeysql driver value)))
