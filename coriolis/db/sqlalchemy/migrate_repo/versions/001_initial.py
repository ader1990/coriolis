# Copyright 2016 Cloudbase Solutions Srl
# All Rights Reserved.

import uuid

import sqlalchemy


def upgrade(migrate_engine):
    meta = sqlalchemy.MetaData()
    meta.bind = migrate_engine

    base_transfer_action = sqlalchemy.Table(
        'base_transfer_action', meta,
        sqlalchemy.Column("base_id", sqlalchemy.String(36), primary_key=True,
                          default=lambda: str(uuid.uuid4())),
        sqlalchemy.Column('created_at', sqlalchemy.DateTime),
        sqlalchemy.Column('updated_at', sqlalchemy.DateTime),
        sqlalchemy.Column('deleted_at', sqlalchemy.DateTime),
        sqlalchemy.Column('deleted', sqlalchemy.String(36)),
        sqlalchemy.Column("user_id", sqlalchemy.String(255), nullable=False),
        sqlalchemy.Column("project_id", sqlalchemy.String(255),
                          nullable=False),
        sqlalchemy.Column("origin", sqlalchemy.Text, nullable=False),
        sqlalchemy.Column("destination", sqlalchemy.Text,
                          nullable=False),
        sqlalchemy.Column("instances", sqlalchemy.Text, nullable=False),
        sqlalchemy.Column("type", sqlalchemy.String(50), nullable=False),
        sqlalchemy.Column("info", sqlalchemy.Text, nullable=False),
        mysql_engine='InnoDB',
        mysql_charset='utf8'
    )

    migration = sqlalchemy.Table(
        'migration', meta,
        sqlalchemy.Column("id", sqlalchemy.String(36),
                          sqlalchemy.ForeignKey(
                              'base_transfer_action.base_id'),
                          primary_key=True),
        sqlalchemy.Column("replica_id", sqlalchemy.String(36),
                          sqlalchemy.ForeignKey(
                              'replica.id'), nullable=True),
        mysql_engine='InnoDB',
        mysql_charset='utf8'
    )

    task = sqlalchemy.Table(
        'task', meta, sqlalchemy.Column(
            'id', sqlalchemy.String(36),
            primary_key=True, default=lambda: str(uuid.uuid4())),
        sqlalchemy.Column('created_at', sqlalchemy.DateTime),
        sqlalchemy.Column('updated_at', sqlalchemy.DateTime),
        sqlalchemy.Column('deleted_at', sqlalchemy.DateTime),
        sqlalchemy.Column('deleted', sqlalchemy.String(36)),
        sqlalchemy.Column(
            "execution_id", sqlalchemy.String(36),
            sqlalchemy.ForeignKey('tasks_execution.id'),
            nullable=False),
        sqlalchemy.Column(
            "instance", sqlalchemy.String(1024),
            nullable=False),
        sqlalchemy.Column(
            "host", sqlalchemy.String(1024),
            nullable=True),
        sqlalchemy.Column(
            "process_id", sqlalchemy.Integer, nullable=True),
        sqlalchemy.Column(
            "status", sqlalchemy.String(100),
            nullable=False),
        sqlalchemy.Column(
            "task_type", sqlalchemy.String(100),
            nullable=False),
        sqlalchemy.Column(
            "exception_details", sqlalchemy.Text, nullable=True),
        sqlalchemy.Column("depends_on", sqlalchemy.Text, nullable=True),
        sqlalchemy.Column("on_error", sqlalchemy.Boolean, nullable=True),
        mysql_engine='InnoDB', mysql_charset='utf8')

    tasks_execution = sqlalchemy.Table(
        'tasks_execution', meta,
        sqlalchemy.Column('id', sqlalchemy.String(36), primary_key=True,
                          default=lambda: str(uuid.uuid4())),
        sqlalchemy.Column('created_at', sqlalchemy.DateTime),
        sqlalchemy.Column('updated_at', sqlalchemy.DateTime),
        sqlalchemy.Column('deleted_at', sqlalchemy.DateTime),
        sqlalchemy.Column('deleted', sqlalchemy.String(36)),
        sqlalchemy.Column("action_id", sqlalchemy.String(36),
                          sqlalchemy.ForeignKey(
                              'base_transfer_action.base_id'),
                          nullable=False),
        sqlalchemy.Column("status", sqlalchemy.String(100), nullable=False),
        sqlalchemy.Column("number", sqlalchemy.Integer, nullable=False),
        mysql_engine='InnoDB',
        mysql_charset='utf8'
    )

    task_progress_update = sqlalchemy.Table(
        'task_progress_update', meta,
        sqlalchemy.Column('id', sqlalchemy.String(36), primary_key=True,
                          default=lambda: str(uuid.uuid4())),
        sqlalchemy.Column('created_at', sqlalchemy.DateTime),
        sqlalchemy.Column('updated_at', sqlalchemy.DateTime),
        sqlalchemy.Column('deleted_at', sqlalchemy.DateTime),
        sqlalchemy.Column('deleted', sqlalchemy.String(36)),
        sqlalchemy.Column("task_id", sqlalchemy.String(36),
                          sqlalchemy.ForeignKey('task.id'),
                          nullable=False),
        sqlalchemy.Column("current_step", sqlalchemy.Integer, nullable=False),
        sqlalchemy.Column("total_steps", sqlalchemy.Integer, nullable=True),
        sqlalchemy.Column("message", sqlalchemy.String(1024), nullable=True),
        mysql_engine='InnoDB',
        mysql_charset='utf8'
    )

    task_events = sqlalchemy.Table(
        'task_event', meta,
        sqlalchemy.Column('id', sqlalchemy.String(36), primary_key=True,
                          default=lambda: str(uuid.uuid4())),
        sqlalchemy.Column('created_at', sqlalchemy.DateTime),
        sqlalchemy.Column('updated_at', sqlalchemy.DateTime),
        sqlalchemy.Column('deleted_at', sqlalchemy.DateTime),
        sqlalchemy.Column('deleted', sqlalchemy.String(36)),
        sqlalchemy.Column("task_id", sqlalchemy.String(36),
                          sqlalchemy.ForeignKey('task.id'),
                          nullable=False),
        sqlalchemy.Column("level", sqlalchemy.String(50), nullable=False),
        sqlalchemy.Column("message", sqlalchemy.String(1024), nullable=False),
        mysql_engine='InnoDB',
        mysql_charset='utf8'
    )

    replica = sqlalchemy.Table(
        'replica', meta,
        sqlalchemy.Column("id", sqlalchemy.String(36),
                          sqlalchemy.ForeignKey(
                              'base_transfer_action.base_id'),
                          primary_key=True),
        mysql_engine='InnoDB',
        mysql_charset='utf8'
    )

    tables = (
        base_transfer_action,
        replica,
        migration,
        tasks_execution,
        task,
        task_progress_update,
        task_events,
    )

    for index, table in enumerate(tables):
        try:
            table.create()
        except Exception:
            # If an error occurs, drop all tables created so far to return
            # to the previously existing state.
            meta.drop_all(tables=tables[:index])
            raise
