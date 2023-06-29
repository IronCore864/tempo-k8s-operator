# Copyright 2022 Pietro Pasotti
# See LICENSE file for licensing details.
"""## Overview.

This document explains how to integrate with the Tempo charm for the purpose of providing a
tracing endpoint to Tempo. It also explains how alternative implementations of the Tempo charm
may maintain the same interface and be backward compatible with all currently integrated charms.

## Provider Library Usage

This Tempo charm interacts with its scrape targets using its charm library. Charms seeking to
expose tracing endpoints for the Tempo charm, must do so using the `TracingEndpointProvider`
object from this charm library. For the simplest use cases, using the `TracingEndpointProvider`
object only requires instantiating it, typically in the constructor of your charm. The
`TracingEndpointProvider` constructor requires the name of the relation over which a scrape
target (tracing endpoint) is exposed to the Tempo charm. This relation must use the
`tempo_scrape` interface. By default address of the tracing endpoint is set to the unit IP
address, by each unit of the `TracingEndpointProvider` charm. These units set their address in
response to the `PebbleReady` event of each container in the unit, since container restarts of
Kubernetes charms can result in change of IP addresses. The default name for the tracing endpoint
relation is `tracing`. It is strongly recommended to use the same relation name for
consistency across charms and doing so obviates the need for an additional constructor argument.
The `TracingEndpointProvider` object may be instantiated as follows

    from charms.tempo.v0.tempo_scrape import TracingEndpointProvider

    def __init__(self, *args):
        super().__init__(*args)
        # ...
        self.tracing_endpoint = TracingEndpointProvider(self)
        # ...

Note that the first argument (`self`) to `TracingEndpointProvider` is always a reference to the
parent (scrape target) charm.

An instantiated `TracingEndpointProvider` object will ensure that each unit of its parent charm,
is a scrape target for the `TracingEndpointConsumer` (Tempo) charm. By default
`TracingEndpointProvider` assumes each unit of the consumer charm exports its profiles on port
80. These defaults may be changed by providing the `TracingEndpointProvider` constructor an
optional argument (`jobs`) that represents a Tempo scrape job specification using Python standard
data structures. This job specification is a subset of Tempo's own [scrape
configuration](https://www.tempo.dev/docs/configuration) format but represented using Python data
structures. More than one job may be provided using the `jobs` argument. Hence `jobs` accepts a
list of dictionaries where each dictionary represents one `<scrape_config>` object as described in
the Tempo documentation. The currently supported configuration subset is: `job_name`,
`static_configs`

Suppose it is required to change the port on which scraped profiles are exposed to 8000. This may be
done by providing the following data structure as the value of `jobs`.

```python
[{"static_configs": [{"targets": ["*:8000"]}]}]
```

The wildcard ("*") host specification implies that the scrape targets will automatically be set to
the host addresses advertised by each unit of the consumer charm.

It is also possible to change the profile path and scrape multiple ports, for example

```
[{"static_configs": [{"targets": ["*:8000", "*:8081"]}]}]
```

More complex scrape configurations are possible. For example

```
[{
    "static_configs": [{
        "targets": ["10.1.32.215:7000", "*:8000"],
        "labels": {
            "some-key": "some-value"
        }
    }]
}]
```

This example scrapes the target "10.1.32.215" at port 7000 in addition to scraping each unit at
port 8000. There is however one difference between wildcard targets (specified using "*") and fully
qualified targets (such as "10.1.32.215"). The Tempo charm automatically associates labels with
profiles generated by each target. These labels localise the source of profiles within the Juju
topology by specifying its "model name", "model UUID", "application name" and "unit name". However
unit name is associated only with wildcard targets but not with fully qualified targets.

Multiple jobs with labels are allowed, but each job must be given a unique name:

```
[
    {
        "job_name": "my-first-job",
        "static_configs": [
            {
                "targets": ["*:7000"],
                "labels": {
                    "some-key": "some-value"
                }
            }
        ]
    },
    {
        "job_name": "my-second-job",
        "static_configs": [
            {
                "targets": ["*:8000"],
                "labels": {
                    "some-other-key": "some-other-value"
                }
            }
        ]
    }
]
```

**Important:** `job_name` should be a fixed string (e.g. hardcoded literal). For instance, if you
include variable elements, like your `unit.name`, it may break the continuity of the profile time
series gathered by Tempo when the leader unit changes (e.g. on upgrade or rescale).

## Consumer Library Usage

The `TracingEndpointConsumer` object may be used by Tempo charms to manage relations with their
scrape targets. For this purposes a Tempo charm needs to do two things

1. Instantiate the `TracingEndpointConsumer` object by providing it a
reference to the parent (Tempo) charm and optionally the name of the relation that the Tempo charm
uses to interact with scrape targets. This relation must confirm to the `tempo_scrape` interface
and it is strongly recommended that this relation be named `tracing` which is its
default value.

For example a Tempo charm may instantiate the `TracingEndpointConsumer` in its constructor as
follows

    from charms.tempo.v0.tempo_scrape import TracingEndpointConsumer

    def __init__(self, *args):
        super().__init__(*args)
        # ...
        self.tracing_consumer = TracingEndpointConsumer(self)
        # ...

2. A Tempo charm also needs to respond to the `TargetsChangedEvent` event of the
`TracingEndpointConsumer` by adding itself as an observer for these events, as in

    self.framework.observe(
        self.tracing_consumer.on.targets_changed,
        self._on_scrape_targets_changed,
    )

In responding to the `TargetsChangedEvent` event the Tempo charm must update the Tempo
configuration so that any new scrape targets are added and/or old ones removed from the list of
scraped endpoints. For this purpose the `TracingEndpointConsumer` object exposes a `jobs()`
method that returns a list of scrape jobs. Each element of this list is the Tempo scrape
configuration for that job. In order to update the Tempo configuration, the Tempo charm needs to
replace the current list of jobs with the list provided by `jobs()` as follows

    def _on_scrape_targets_changed(self, event):
        ...
        scrape_jobs = self.tracing_consumer.jobs()
        for job in scrape_jobs:
            tempo_scrape_config.append(job)
        ...

## Relation Data

Units of profiles provider charms advertise their names and addresses over unit relation data using
the `tempo_scrape_unit_name` and `tempo_scrape_unit_address` keys. While the `scrape_metadata`,
`scrape_jobs` and `alert_rules` keys in application relation data of profiles provider charms hold
eponymous information.

"""  # noqa: W505
import json
import logging
from itertools import starmap
from typing import Optional, Tuple, Dict, Any, Literal, TYPE_CHECKING, List

from ops.charm import CharmBase, RelationRole, CharmEvents, RelationEvent
from ops.framework import EventSource, Object, ObjectEvents
from ops.model import ModelError, Relation
from pydantic import BaseModel, AnyHttpUrl, Json

# The unique Charmhub library identifier, never change it
LIBID = "7b30b495435746acb645ca414898621f"

# Increment this major API version when introducing breaking changes
LIBAPI = 0

# Increment this PATCH version before using `charmcraft publish-lib` or reset
# to 0 if you are raising the major API version
LIBPATCH = 3

PYDEPS = ["pydantic"]

logger = logging.getLogger(__name__)

ALLOWED_KEYS = {"job_name", "static_configs", "scrape_interval", "scrape_timeout"}
DEFAULT_JOB = {"static_configs": [{"targets": ["*:80"]}]}
DEFAULT_RELATION_NAME = "tracing"
RELATION_INTERFACE_NAME = "tracing"

IngesterType = Literal['otlp_grpc', 'otlp_http', 'zipkin', 'tempo']


class Ingester(BaseModel):
    port: str
    type: IngesterType


class TracingRequirerData(BaseModel):
    hostname: AnyHttpUrl
    ingesters: Json[List[Ingester]]


class _AutoSnapshotEvent(RelationEvent):
    __args__ = ()  # type: Tuple[str, ...]
    __optional_kwargs__ = {}  # type: Dict[str, Any]

    @classmethod
    def __attrs__(cls):
        return cls.__args__ + tuple(cls.__optional_kwargs__.keys())

    def __init__(self, handle, relation, *args, **kwargs):
        super().__init__(handle, relation)

        if not len(self.__args__) == len(args):
            raise TypeError("expected {} args, got {}".format(len(self.__args__), len(args)))

        for attr, obj in zip(self.__args__, args):
            setattr(self, attr, obj)
        for attr, default in self.__optional_kwargs__.items():
            obj = kwargs.get(attr, default)
            setattr(self, attr, obj)

    def snapshot(self) -> dict:
        dct = super().snapshot()
        for attr in self.__attrs__():
            obj = getattr(self, attr)
            try:
                dct[attr] = obj
            except ValueError as e:
                raise ValueError(
                    "cannot automagically serialize {}: "
                    "override this method and do it "
                    "manually.".format(obj)
                ) from e

        return dct

    def restore(self, snapshot: dict) -> None:
        super().restore(snapshot)
        for attr, obj in snapshot.items():
            setattr(self, attr, obj)


class RelationNotFoundError(Exception):
    """Raised if no relation with the given name is found."""

    def __init__(self, relation_name: str):
        self.relation_name = relation_name
        self.message = "No relation named '{}' found".format(relation_name)
        super().__init__(self.message)


class RelationInterfaceMismatchError(Exception):
    """Raised if the relation with the given name has an unexpected interface."""

    def __init__(
            self,
            relation_name: str,
            expected_relation_interface: str,
            actual_relation_interface: str,
    ):
        self.relation_name = relation_name
        self.expected_relation_interface = expected_relation_interface
        self.actual_relation_interface = actual_relation_interface
        self.message = (
            "The '{}' relation has '{}' as interface rather than the expected '{}'".format(
                relation_name, actual_relation_interface, expected_relation_interface
            )
        )

        super().__init__(self.message)


class RelationRoleMismatchError(Exception):
    """Raised if the relation with the given name has a different role than expected."""

    def __init__(
            self,
            relation_name: str,
            expected_relation_role: RelationRole,
            actual_relation_role: RelationRole,
    ):
        self.relation_name = relation_name
        self.expected_relation_interface = expected_relation_role
        self.actual_relation_role = actual_relation_role
        self.message = "The '{}' relation has role '{}' rather than the expected '{}'".format(
            relation_name, repr(actual_relation_role), repr(expected_relation_role)
        )

        super().__init__(self.message)


def _validate_relation_by_interface_and_direction(
        charm: CharmBase,
        relation_name: str,
        expected_relation_interface: str,
        expected_relation_role: RelationRole,
):
    """Verifies that a relation has the necessary characteristics.

    Verifies that the `relation_name` provided: (1) exists in metadata.yaml,
    (2) declares as interface the interface name passed as `relation_interface`
    and (3) has the right "direction", i.e., it is a relation that `charm`
    provides or requires.

    Args:
        charm: a `CharmBase` object to scan for the matching relation.
        relation_name: the name of the relation to be verified.
        expected_relation_interface: the interface name to be matched by the
            relation named `relation_name`.
        expected_relation_role: whether the `relation_name` must be either
            provided or required by `charm`.

    Raises:
        RelationNotFoundError: If there is no relation in the charm's metadata.yaml
            with the same name as provided via `relation_name` argument.
        RelationInterfaceMismatchError: The relation with the same name as provided
            via `relation_name` argument does not have the same relation interface
            as specified via the `expected_relation_interface` argument.
        RelationRoleMismatchError: If the relation with the same name as provided
            via `relation_name` argument does not have the same role as specified
            via the `expected_relation_role` argument.
    """
    if relation_name not in charm.meta.relations:
        raise RelationNotFoundError(relation_name)

    relation = charm.meta.relations[relation_name]

    actual_relation_interface = relation.interface_name
    if actual_relation_interface != expected_relation_interface:
        raise RelationInterfaceMismatchError(
            relation_name, expected_relation_interface, actual_relation_interface
        )

    if expected_relation_role is RelationRole.provides:
        if relation_name not in charm.meta.provides:
            raise RelationRoleMismatchError(
                relation_name, RelationRole.provides, RelationRole.requires
            )
    elif expected_relation_role is RelationRole.requires:
        if relation_name not in charm.meta.requires:
            raise RelationRoleMismatchError(
                relation_name, RelationRole.requires, RelationRole.provides
            )
    else:
        raise TypeError("Unexpected RelationDirection: {}".format(expected_relation_role))


class TargetsChangedEvent(_AutoSnapshotEvent):
    """Event emitted when Tempo scrape targets change."""
    __args__ = ("relation_id", )


class MonitoringEvents(ObjectEvents):
    """Event descriptor for events raised by `TracingEndpointConsumer`."""

    targets_changed = EventSource(TargetsChangedEvent)


class TracingEndpointRequirer(Object):
    """A Tempo based monitoring service."""

    on = MonitoringEvents()

    def __init__(self,
                 charm: CharmBase,
                 hostname: str,
                 ingesters: List[Ingester],
                 relation_name: str = DEFAULT_RELATION_NAME):
        """A Tempo based Monitoring service.

        Args:
            charm: a `CharmBase` instance that manages this instance of the Tempo service.
            relation_name: an optional string name of the relation between `charm`
                and the Tempo charmed service. The default is "tracing".

        Raises:
            RelationNotFoundError: If there is no relation in the charm's metadata.yaml
                with the same name as provided via `relation_name` argument.
            RelationInterfaceMismatchError: The relation with the same name as provided
                via `relation_name` argument does not have the `tempo_scrape` relation
                interface.
            RelationRoleMismatchError: If the relation with the same name as provided
                via `relation_name` argument does not have the `RelationRole.requires`
                role.
        """
        _validate_relation_by_interface_and_direction(
            charm, relation_name, RELATION_INTERFACE_NAME, RelationRole.requires
        )

        super().__init__(charm, relation_name)
        self._charm = charm
        self._hostname = hostname
        self._ingesters = ingesters
        self._relation_name = relation_name
        events = self._charm.on[relation_name]
        self.framework.observe(
            events.relation_created, self.update_relation_data
        )
        self.framework.observe(
            events.relation_joined, self.update_relation_data
        )

    def update_relation_data(self, _):
        try:
            if self._charm.unit.is_leader():
                for relation in self._charm.model.relations[self._relation_name]:
                    app_databag = relation.data[self._charm.app]
                    app_databag['hostname'] = self._hostname
                    app_databag['ingesters'] = json.dumps([ing.dict() for ing in self._ingesters])

        except ModelError as e:
            # args are bytes
            if e.args[0].startswith(b'ERROR cannot read relation application '
                                    b'settings: permission denied'):
                logger.error(f"encountered error {e} while attempting to update_relation_data."
                             f"The relation must be gone.")
                return


class EndpointChangedEvent(_AutoSnapshotEvent):
    __args__ = ("hostname", "ingesters")

    if TYPE_CHECKING:
        hostname = ""  # type: str
        ingesters = []  # type: List[Ingester]


class TracingEndpointEvents(CharmEvents):
    endpoint_changed = EventSource(EndpointChangedEvent)


class TracingEndpointProvider(Object):
    """A tracing endpoint for Tempo."""

    on = TracingEndpointEvents()

    def __init__(
            self,
            charm: CharmBase,
            relation_name: str = DEFAULT_RELATION_NAME,
    ):
        """Construct a tracing provider for a Tempo charm.

        If your charm exposes a Tempo tracing endpoint, the `TracingEndpointProvider` object
        enables your charm to easily communicate how to reach that endpoint.

        By default, a charm instantiating this object has the tracing endpoints of each of its
        units scraped by the related Tempo charms.

        The scraped profiles are automatically tagged by the Tempo charms with Juju topology data
        via the `juju_model_name`, `juju_model_uuid`, `juju_application_name` and `juju_unit`
        labels. To support such tagging `TracingEndpointProvider` automatically forwards scrape
        metadata to a `TracingEndpointConsumer` (Tempo charm).

        Scrape targets provided by `TracingEndpointProvider` can be customized when instantiating
        this object. For example in the case of a charm exposing the tracing endpoint for each of
        its units on port 8080, the `TracingEndpointProvider` can be
        instantiated as follows:

            self.tracing_endpoint_provider = TracingEndpointProvider(
                self, jobs=[{"static_configs": [{"targets": ["*:8080"]}]}]
            )

        The notation `*:<port>` means "scrape each unit of this charm on port `<port>`.

        Args:
            charm: a `CharmBase` object that manages this
                `TracingEndpointProvider` object. Typically this is `self` in the instantiating
                class.
            relation_name: an optional string name of the relation between `charm`
                and the Tempo charmed service. The default is "tracing". It is strongly
                advised not to change the default, so that people deploying your charm will have a
                consistent experience with all other charms that provide tracing endpoints.

        Raises:
            RelationNotFoundError: If there is no relation in the charm's metadata.yaml
                with the same name as provided via `relation_name` argument.
            RelationInterfaceMismatchError: The relation with the same name as provided
                via `relation_name` argument does not have the `tempo_scrape` relation
                interface.
            RelationRoleMismatchError: If the relation with the same name as provided
                via `relation_name` argument does not have the `RelationRole.provides`
                role.
        """
        _validate_relation_by_interface_and_direction(
            charm, relation_name, RELATION_INTERFACE_NAME, RelationRole.provides
        )

        super().__init__(charm, relation_name)
        self._charm = charm
        self._relation_name = relation_name

        events = self._charm.on[self._relation_name]
        self.framework.observe(events.relation_changed, self._on_tracing_relation_changed)

    def _on_tracing_relation_changed(self, event):
        """Notify the providers that there is new endpoint information available.
        """
        data = self._deserialize_from_relation(event.relation)
        if data:
            self.on.endpoint_changed.emit(event.relation, data.hostname, data.ingesters)

    def _deserialize_from_relation(self, relation: Relation) -> Optional[TracingRequirerData]:
        try:
            app_databag = relation.data[relation.app]
            data = app_databag.get('hostname')
            ingesters = list(starmap(Ingester, json.loads(app_databag.get('ingesters'))))
            return TracingRequirerData(hostname=data, ingesters=ingesters)
        except Exception as e:
            logger.error(e, exc_info=True)
            return None

    @property
    def endpoint(self) -> Optional[TracingRequirerData]:
        try:
            relation = self._charm.model.get_relation(self._relation_name)
            return self._deserialize_from_relation(relation)
        except Exception as e:
            logger.error(f"Unable to fetch tempo endpoint from relation data: {e}")
            return None

    @property
    def otlp_grpc_endpoint(self) -> Optional[str]:
        ep = self.endpoint
        if not ep:
            return None
        otlp_grpc_ingester_port = next(filter(lambda i: i.type == "otlp_grpc", ep.ingesters)).port
        return f"http://{ep.hostname}:{otlp_grpc_ingester_port}"
