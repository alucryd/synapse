# -*- coding: utf-8 -*-
# Copyright 2014-2016 OpenMarket Ltd
# Copyright 2018 New Vector Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
from typing import List, Tuple

from canonicaljson import json

from twisted.internet import defer

from synapse.storage.data_stores.main.account_data import AccountDataWorkerStore
from synapse.util.caches.descriptors import cached

logger = logging.getLogger(__name__)


class TagsWorkerStore(AccountDataWorkerStore):
    @cached()
    def get_tags_for_user(self, user_id):
        """Get all the tags for a user.


        Args:
            user_id(str): The user to get the tags for.
        Returns:
            A deferred dict mapping from room_id strings to dicts mapping from
            tag strings to tag content.
        """

        deferred = self.db.simple_select_list(
            "room_tags", {"user_id": user_id}, ["room_id", "tag", "content"]
        )

        @deferred.addCallback
        def tags_by_room(rows):
            tags_by_room = {}
            for row in rows:
                room_tags = tags_by_room.setdefault(row["room_id"], {})
                room_tags[row["tag"]] = json.loads(row["content"])
            return tags_by_room

        return deferred

    async def get_all_updated_tags(
        self, instance_name: str, last_id: int, current_id: int, limit: int
    ) -> Tuple[List[Tuple[int, tuple]], int, bool]:
        """Get updates for tags replication stream.

        Args:
            instance_name: The writer we want to fetch updates from. Unused
                here since there is only ever one writer.
            last_id: The token to fetch updates from. Exclusive.
            current_id: The token to fetch updates up to. Inclusive.
            limit: The requested limit for the number of rows to return. The
                function may return more or fewer rows.

        Returns:
            A tuple consisting of: the updates, a token to use to fetch
            subsequent updates, and whether we returned fewer rows than exists
            between the requested tokens due to the limit.

            The token returned can be used in a subsequent call to this
            function to get further updatees.

            The updates are a list of 2-tuples of stream ID and the row data
        """

        if last_id == current_id:
            return [], current_id, False

        def get_all_updated_tags_txn(txn):
            sql = (
                "SELECT stream_id, user_id, room_id"
                " FROM room_tags_revisions as r"
                " WHERE ? < stream_id AND stream_id <= ?"
                " ORDER BY stream_id ASC LIMIT ?"
            )
            txn.execute(sql, (last_id, current_id, limit))
            return txn.fetchall()

        tag_ids = await self.db.runInteraction(
            "get_all_updated_tags", get_all_updated_tags_txn
        )

        def get_tag_content(txn, tag_ids):
            sql = "SELECT tag, content FROM room_tags WHERE user_id=? AND room_id=?"
            results = []
            for stream_id, user_id, room_id in tag_ids:
                txn.execute(sql, (user_id, room_id))
                tags = []
                for tag, content in txn:
                    tags.append(json.dumps(tag) + ":" + content)
                tag_json = "{" + ",".join(tags) + "}"
                results.append((stream_id, (user_id, room_id, tag_json)))

            return results

        batch_size = 50
        results = []
        for i in range(0, len(tag_ids), batch_size):
            tags = await self.db.runInteraction(
                "get_all_updated_tag_content",
                get_tag_content,
                tag_ids[i : i + batch_size],
            )
            results.extend(tags)

        limited = False
        upto_token = current_id
        if len(results) >= limit:
            upto_token = results[-1][0]
            limited = True

        return results, upto_token, limited

    @defer.inlineCallbacks
    def get_updated_tags(self, user_id, stream_id):
        """Get all the tags for the rooms where the tags have changed since the
        given version

        Args:
            user_id(str): The user to get the tags for.
            stream_id(int): The earliest update to get for the user.
        Returns:
            A deferred dict mapping from room_id strings to lists of tag
            strings for all the rooms that changed since the stream_id token.
        """

        def get_updated_tags_txn(txn):
            sql = (
                "SELECT room_id from room_tags_revisions"
                " WHERE user_id = ? AND stream_id > ?"
            )
            txn.execute(sql, (user_id, stream_id))
            room_ids = [row[0] for row in txn]
            return room_ids

        changed = self._account_data_stream_cache.has_entity_changed(
            user_id, int(stream_id)
        )
        if not changed:
            return {}

        room_ids = yield self.db.runInteraction(
            "get_updated_tags", get_updated_tags_txn
        )

        results = {}
        if room_ids:
            tags_by_room = yield self.get_tags_for_user(user_id)
            for room_id in room_ids:
                results[room_id] = tags_by_room.get(room_id, {})

        return results

    def get_tags_for_room(self, user_id, room_id):
        """Get all the tags for the given room
        Args:
            user_id(str): The user to get tags for
            room_id(str): The room to get tags for
        Returns:
            A deferred list of string tags.
        """
        return self.db.simple_select_list(
            table="room_tags",
            keyvalues={"user_id": user_id, "room_id": room_id},
            retcols=("tag", "content"),
            desc="get_tags_for_room",
        ).addCallback(
            lambda rows: {row["tag"]: json.loads(row["content"]) for row in rows}
        )


class TagsStore(TagsWorkerStore):
    @defer.inlineCallbacks
    def add_tag_to_room(self, user_id, room_id, tag, content):
        """Add a tag to a room for a user.
        Args:
            user_id(str): The user to add a tag for.
            room_id(str): The room to add a tag for.
            tag(str): The tag name to add.
            content(dict): A json object to associate with the tag.
        Returns:
            A deferred that completes once the tag has been added.
        """
        content_json = json.dumps(content)

        def add_tag_txn(txn, next_id):
            self.db.simple_upsert_txn(
                txn,
                table="room_tags",
                keyvalues={"user_id": user_id, "room_id": room_id, "tag": tag},
                values={"content": content_json},
            )
            self._update_revision_txn(txn, user_id, room_id, next_id)

        with self._account_data_id_gen.get_next() as next_id:
            yield self.db.runInteraction("add_tag", add_tag_txn, next_id)

        self.get_tags_for_user.invalidate((user_id,))

        result = self._account_data_id_gen.get_current_token()
        return result

    @defer.inlineCallbacks
    def remove_tag_from_room(self, user_id, room_id, tag):
        """Remove a tag from a room for a user.
        Returns:
            A deferred that completes once the tag has been removed
        """

        def remove_tag_txn(txn, next_id):
            sql = (
                "DELETE FROM room_tags "
                " WHERE user_id = ? AND room_id = ? AND tag = ?"
            )
            txn.execute(sql, (user_id, room_id, tag))
            self._update_revision_txn(txn, user_id, room_id, next_id)

        with self._account_data_id_gen.get_next() as next_id:
            yield self.db.runInteraction("remove_tag", remove_tag_txn, next_id)

        self.get_tags_for_user.invalidate((user_id,))

        result = self._account_data_id_gen.get_current_token()
        return result

    def _update_revision_txn(self, txn, user_id, room_id, next_id):
        """Update the latest revision of the tags for the given user and room.

        Args:
            txn: The database cursor
            user_id(str): The ID of the user.
            room_id(str): The ID of the room.
            next_id(int): The the revision to advance to.
        """

        txn.call_after(
            self._account_data_stream_cache.entity_has_changed, user_id, next_id
        )

        # Note: This is only here for backwards compat to allow admins to
        # roll back to a previous Synapse version. Next time we update the
        # database version we can remove this table.
        update_max_id_sql = (
            "UPDATE account_data_max_stream_id"
            " SET stream_id = ?"
            " WHERE stream_id < ?"
        )
        txn.execute(update_max_id_sql, (next_id, next_id))

        update_sql = (
            "UPDATE room_tags_revisions"
            " SET stream_id = ?"
            " WHERE user_id = ?"
            " AND room_id = ?"
        )
        txn.execute(update_sql, (next_id, user_id, room_id))

        if txn.rowcount == 0:
            insert_sql = (
                "INSERT INTO room_tags_revisions (user_id, room_id, stream_id)"
                " VALUES (?, ?, ?)"
            )
            try:
                txn.execute(insert_sql, (user_id, room_id, next_id))
            except self.database_engine.module.IntegrityError:
                # Ignore insertion errors. It doesn't matter if the row wasn't
                # inserted because if two updates happend concurrently the one
                # with the higher stream_id will not be reported to a client
                # unless the previous update has completed. It doesn't matter
                # which stream_id ends up in the table, as long as it is higher
                # than the id that the client has.
                pass
