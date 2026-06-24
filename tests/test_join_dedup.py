"""Regression tests for join de-duplication across search + header-filter paths.

Bug: when a list request had *both* a global search term and a header filter
that targeted the same FK relationship (e.g. ``serverid.hostname`` present in
both ``column_searchable`` and ``column_header_filters``), the search path and
the header-filter path each emitted a ``LEFT OUTER JOIN`` for the same table.
Postgres rejected the duplicate alias with
``psycopg2.errors.DuplicateAlias: table name "server" specified more than once``
(SQLite raises ``ambiguous column name``). See ``_joined_table_names`` and the
header-filter blocks in ``crud.py``.
"""

import os

# Must be set before importing fasthx_admin.auth (it reads the env at import).
os.environ["AUTH_DISABLED"] = "1"

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Column, Integer, String, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.pool import StaticPool

from fasthx_admin import Admin, CRUDView
from fasthx_admin.database import Base, init_db, get_db
from fasthx_admin.crud import _joined_table_names


class Server(Base):
    __tablename__ = "server"
    id = Column(Integer, primary_key=True)
    hostname = Column(String)
    serialnum = Column(String)


class Offering(Base):
    __tablename__ = "offering"
    id = Column(Integer, primary_key=True)
    sid = Column(String)
    serverid = Column(Integer, ForeignKey("server.id"))
    # Relationship lets the framework auto-register Server in _model_registry
    # even though it has no CRUDView of its own.
    server = relationship("Server")


class OfferingView(CRUDView):
    model = Offering
    name = "offering"
    column_list = ["id", "sid", "serverid"]
    # The same FK relationship in both lists is what used to double-join.
    column_searchable = ["sid", "serverid.hostname"]
    column_header_filters = ["serverid.hostname"]


@pytest.fixture()
def client():
    engine = init_db(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,  # single shared in-memory connection
    )
    Base.metadata.create_all(engine)

    db = next(get_db())
    db.add(Server(id=1, hostname="AU-Coomera-A1", serialnum="SN1"))
    db.add(Offering(id=1, sid="slid-1", serverid=1))
    db.commit()
    db.close()

    app = FastAPI()
    admin = Admin(app)
    admin.add_view(OfferingView)
    yield TestClient(app)

    Base.metadata.drop_all(engine)


def test_search_and_header_filter_together_does_not_double_join(client):
    """The regression: search term + header filter on the same FK relationship.

    Before the fix this raised a 500 (DuplicateAlias / ambiguous column);
    after the fix the table is joined once and the page renders.
    """
    resp = client.get("/offering", params={"q": "slid", "cf_serverid.hostname": "Coomera"})
    assert resp.status_code == 200, resp.text


def test_search_only_still_works(client):
    resp = client.get("/offering", params={"q": "slid"})
    assert resp.status_code == 200, resp.text


def test_header_filter_only_still_works(client):
    resp = client.get("/offering", params={"cf_serverid.hostname": "Coomera"})
    assert resp.status_code == 200, resp.text


def test_header_filter_value_is_stripped(client):
    """A stray leading tab (seen in the wild from Select2) must not break the match."""
    resp = client.get("/offering", params={"q": "slid", "cf_serverid.hostname": "\tCoomera"})
    assert resp.status_code == 200, resp.text


def test_joined_table_names_detects_outerjoin():
    """Unit test for the introspection helper underpinning the fix."""
    engine = init_db(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = next(get_db())
    try:
        q = db.query(Offering)
        assert _joined_table_names(q) == {"offering"}
        q = q.outerjoin(Server, Offering.serverid == Server.id)
        assert _joined_table_names(q) == {"offering", "server"}
    finally:
        db.close()
        Base.metadata.drop_all(engine)
