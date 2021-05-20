#!/usr/bin/env python3

import resource
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

# some languages that don't use an alphabet are put apart as they are likely
# to have a lot of different ngrams and by so need to have a lower limit for
# the ngrams we kept for that languages
IDEOGRAM_LANGS = ("wuu", "yue", "cmn")
IDEOGRAM_NGRAM_FREQ_LIMIT = 0.000005
NGRAM_FREQ_LIMIT = 0.00001
# minimum number of characters a user must have submitted in one language to
# be considered as a key contributor in this language (note that this number
# is currently purely arbitrary)
MIN_USER_CONTRIB_IN_LANG = 100
# minimum and maximum sizes of the n-grams to be generated
MIN_NGRAM_SIZE = 2
MAX_NGRAM_SIZE = 5


class SqliteDB:
    """A generic sqlite database"""

    def __init__(self, db_path: Path) -> None:
        """
        Parameters
        ----------
        db_path : Path
            the location of the sqlite database
        """
        self._fp = db_path

    def delete_file(self) -> None:
        """Delete the sqlite file of this database"""
        if self._fp.exists():
            print(f"Deleting {self._fp.name} database")
            self._fp.unlink()

    @property
    def path(self) -> Path:
        """Get the path of the database file"""

        return self._fp


class RawTatodetectDB(SqliteDB):
    """A sqlite database for tracking all n-grams counts and user
    contributions in the Tatoeba corpus

    It handles:
    -   the occurrence counts of every n-gram found in the
        Tatoeba corpus

    -   the contribution scores of the Tatoeba users in every
        language they have contributed to
    """

    def count(
        self,
        sentences_detailed_path: Path,
        sentence_blacklist: list,
        max_memory: int = 500,
    ) -> None:
        """Count n-grams' occurrences for every Tatoeba language

        A contribution score equal to sum(len(sentences))
        is also computed for every language a user has contributed to.

        All results are gradually saved into database tables in
        order to mitigate the memory footprint of the process.

        Parameters
        ----------
        sentences_detailed_path : Path
            path of the 'sentences_detailed.csv' weekly dump file
        sentence_blacklist : list
            the integer ids of the sentences that are not taken
            into account for this counting
        max_memory : int, optional
            when the process crosses this peak memory usage in megabytes,
            it triggers a buffer flushing, by default 500.
            Note that process speed mainly depends on the amount of data
            written to disk and consequently increases with the buffer size.
        """
        # the former database at this path is overwritten
        self.delete_file()
        # initialize tables
        self._init_db()

        tot_ngrams = 0
        peak_tot_ngrams = None  # number of ngrams at first buffer flush
        user_lang_score = defaultdict(int)
        for n in range(MAX_NGRAM_SIZE, MIN_NGRAM_SIZE - 1, -1):
            table_name = f"grams{n}"
            lang_ngram_cnt = defaultdict(lambda: defaultdict(int))
            with open(sentences_detailed_path, "r", encoding="utf-8") as f:
                for line_id, line in enumerate(f):
                    self._print_status(line_id, ngram_size=n)
                    try:
                        fields = line.rstrip().split("\t")
                        sent_id, lang, text, user = fields[:4]
                    except IndexError:
                        print(f"Skipped erroneous line {line_id}: {line}")
                        continue

                    # sentences with an unset language are ignored
                    if lang == "\\N" or lang == "":
                        continue
                    # sentences with an id matching the blacklist are ignored
                    if int(sent_id) in sentence_blacklist:
                        continue

                    # compute user contribution score during last file reading
                    if n == MIN_NGRAM_SIZE:
                        user_lang_score[(user, lang)] += len(text)

                    # increment hit counts for each ngram in the sentence
                    for i in range(len(text) - n + 1):
                        ngram = text[i : i + n]
                        if ngram not in lang_ngram_cnt[lang]:
                            tot_ngrams += 1
                        lang_ngram_cnt[lang][ngram] += 1

                    # when buffer is full, save it into table and empty it
                    if (
                        peak_tot_ngrams is None
                        and get_peak_memory_usage() >= max_memory
                    ) or (
                        peak_tot_ngrams is not None
                        and tot_ngrams >= peak_tot_ngrams
                    ):
                        self._upsert_ngram_hits(lang_ngram_cnt, table_name)
                        lang_ngram_cnt.clear()
                        peak_tot_ngrams = tot_ngrams
                        tot_ngrams = 0

            self._print_status(line_id, ngram_size=n, force=True)

            # move remaining ngram hits from memory to database tables
            self._upsert_ngram_hits(lang_ngram_cnt, table_name)

            print(" done")

        # save users contribution scores
        self._insert_user_scores(user_lang_score)

    def _init_db(self) -> None:
        """Open the database connection and initialize the tables"""

        with sqlite3.connect(self._fp) as conn:
            # create a table for each n-gram type
            for n in range(MIN_NGRAM_SIZE, MAX_NGRAM_SIZE + 1):
                conn.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS grams{n} (
                      'gram' text not null,
                      'lang'text not null,
                      'hit'  int not null,
                    UNIQUE("gram","lang")
                    );
                    """
                )
            # create a table dedicated to users contribution scores
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users_langs (
                  'user' text not null,
                  'lang' text not null,
                  'total' int not null default 0
                );
                """
            )

    def _upsert_ngram_hits(
        self, lang_ngram_cnt: dict, table_name: str
    ) -> None:
        """Update n-gram hit count values in this table"""

        with sqlite3.connect(self._fp) as conn:
            conn.execute("PRAGMA temp_store=FILE;")
            conn.execute("PRAGMA journal_mode=DELETE;")

            for lang, d in lang_ngram_cnt.items():
                for ngram, hits in d.items():
                    conn.execute(
                        f"""
                        INSERT INTO {table_name}
                          VALUES (:ngram, :lang, :hits) 
                        ON CONFLICT (gram, lang) 
                          DO UPDATE SET hit = hit + :hits;
                        """,
                        {"ngram": ngram, "lang": lang, "hits": hits},
                    )
            conn.execute("PRAGMA shrink_memory;")  # reduce memory load

    def _insert_user_scores(self, user_lang_score: dict) -> None:
        """Insert user language contribution scores into table"""

        print("Inserting users contribution scores")
        with sqlite3.connect(self._fp) as conn:
            for (user, lang), hit in user_lang_score.items():
                conn.execute(
                    "INSERT INTO users_langs VALUES (?,?,?);",
                    (user, lang, hit),
                )
            conn.execute("PRAGMA shrink_memory;")  # reduce memory load

    @staticmethod
    def _print_status(
        line_number: int, ngram_size: int, force: bool = False
    ) -> None:
        """Keep track of n-gram counting progress"""

        if line_number % 10000 == 0 or force:
            msg = (
                f"\rGenerating ngrams of size {ngram_size} "
                f"(reading CSV file... {line_number} lines)"
            )
            print(msg, end="")
            sys.stdout.flush()


class TatodetectDB(SqliteDB):
    """The actual database that is used to detect languages
    on tatoeba.org

    The Tatodetect algorithm is based on the analysis of the
    key n-grams found in a Tatoeba monolingual corpus.

    This database contains only the key content of the
    Tatodetect 'raw database'.
    """

    def extract_key_content_from(self, raw_db: RawTatodetectDB) -> None:
        """Import key content from the Tatodetect raw database

        Parameters
        ----------
        raw_db : RawTatodetectDB
            a database containing all Tatoeba n-grams counts and user
            contribution scores
        """
        # the former Tatodetect database at this path is overwritten
        self.delete_file()
        # Initialize tables
        self._init_db()

        conn = sqlite3.connect(self._fp)
        c = conn.cursor()

        # attach raw database to enable data transfert
        c.execute(f"ATTACH DATABASE '{raw_db.path}' AS raw_db;")

        for n in range(MIN_NGRAM_SIZE, MAX_NGRAM_SIZE + 1):
            print(f"Importing key {n}-grams from raw database")
            # transfer n-grams counts table with extra frequencies
            langs_placeholder = ", ".join(["?"] * len(IDEOGRAM_LANGS))

            c.execute(
                f"""
                INSERT INTO main.grams{n}
                SELECT
                  gram,
                  raw_db.grams{n}.lang,
                  hit,
                  CAST(hit AS FLOAT) / lang_ngram_tots.tot AS percent
                FROM raw_db.grams{n}
                INNER JOIN (
                SELECT
                  lang, 
                  SUM(hit) AS tot
                FROM raw_db.grams{n}
                GROUP BY lang) AS lang_ngram_tots
                ON raw_db.grams{n}.lang = lang_ngram_tots.lang
                WHERE percent > (
                CASE
                  WHEN raw_db.grams{n}.lang IN ({langs_placeholder})
                  THEN ? ELSE ?
                END);
                """,
                (*IDEOGRAM_LANGS, IDEOGRAM_NGRAM_FREQ_LIMIT, NGRAM_FREQ_LIMIT),
            )
            c.execute(f"CREATE INDEX gram_grams{n}_idx ON grams{n}(gram);")

        print(f"Importing key Tatoeba contributors from raw database")
        c.execute(
            """
            INSERT INTO main.users_langs
              SELECT * FROM raw_db.users_langs
              WHERE total > ? ;
            """,
            (MIN_USER_CONTRIB_IN_LANG,),
        )
        c.execute(
            """
            CREATE UNIQUE INDEX lang_user_users_langs_idx 
              ON users_langs(lang,user);
            """
        )
        c.execute("CREATE INDEX user_users_langs_idx ON users_langs(user);")

        conn.commit()

        c.execute("DETACH DATABASE raw_db;")

        c.close()

    def _init_db(self) -> None:
        """Open tha database connection and initialize the tables"""

        with sqlite3.connect(self._fp) as conn:
            # create a table for each n-gram type counts
            for n in range(MIN_NGRAM_SIZE, MAX_NGRAM_SIZE + 1):
                conn.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS grams{n} (
                    'gram' text not null,
                    'lang'text not null,
                    'hit'  int not null,
                    'percent' float not null default 0
                    );
                    """
                )
            # create a table for storing the sentences counts of users
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users_langs (
                  'user' text not null,
                  'lang' text not null ,
                  'total' int not null default 0
                );
                """
            )


def get_sentences_with_tag(tags_path: Path, tag_name: str) -> set:
    """Get the ids of the Tatoeba sentences with this tag

    Parameters
    ----------
    tags_path : Path
        the path of the Tatoeba tags.csv dump file
    tag_name : str
        the tag for which tagged sentences are looked for

    Returns
    -------
    set
        the integer ids of the tagged sentences
    """
    tagged = set()
    try:
        with open(tags_path, "r", encoding="utf-8") as f:
            for line in f:
                sentence_id, tag = line.rstrip().split("\t")
                if tag == tag_name:
                    tagged.add(int(sentence_id))
    except IndexError:
        pass

    return tagged


def get_raw_db_path(db_path: Path) -> Path:
    """Get the path where the raw database associated with this database
    will be saved
    """
    return db_path.parent.joinpath(f"{db_path.stem}_raw{db_path.suffix}")


def get_peak_memory_usage() -> int:
    """Get peak memory usage of current process in megabytes"""
    if sys.platform in ("linux", "darwin"):
        # peak RAM returned in kilobytes on Linux, bytes on OS X
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return peak // 1000 if sys.platform == "linux" else peak // 1000 ** 2

    return


def main():

    max_memory = 500  # peak memory usage of process in megabytes

    # manage CLI inputs
    if len(sys.argv) < 3:
        fnames = "sentences_detailed.csv", "ngrams.db", "tags.csv"
        msg = f"Usage: {sys.argv[0]} <{fnames[0]}> <{fnames[1]}> [{fnames[2]}]"
        print(msg)
        sys.exit(1)

    sentences_detailed_path = Path(sys.argv[1])
    tatodetect_db_path = Path(sys.argv[2])
    try:
        tags_path = Path(sys.argv[3])
    except IndexError:
        tags_path = None

    # the sentences tagged with '@change flag' are blacklisted because
    # they are very likely linked to wrong languages
    sentence_blacklist = get_sentences_with_tag(tags_path, "@change flag")
    # create a database that store all n-grams counts and users scores in the
    # same directory as the smaller final database actually used by Tatodetect
    raw_db_path = get_raw_db_path(tatodetect_db_path)
    raw_db = RawTatodetectDB(raw_db_path)
    raw_db.count(sentences_detailed_path, sentence_blacklist, max_memory)
    # copy most significant content from the raw database to the actual
    # Tatodetect database
    tatodetect_db = TatodetectDB(tatodetect_db_path)
    tatodetect_db.extract_key_content_from(raw_db)
    # delete biggest database file to save disk space
    raw_db.delete_file()


if __name__ == "__main__":

    main()
