# SystemVerilog Package API

Need an efficient SystemVerilog API interface that efficiently
supports both typed and ad-hoc transactions.

## Type-management API
- Query registered types by name -> lookup type handle
-> Note, we should probably support both global and stream-specific type namespaces
- Support type introspection
  - iterate over fields
  - query field attributes (name, type)
- Define a new type

## Stream-creation API
- Create either relative to global or a parent
- Look up stream by name

## Transaction-recording API
- Lifecycle
  - Create a transaction handle -- typed/ad-hoc ; attached to a stream ; always has a start time ; may have a parent
    - Transaction always relative to a stream
  - Set transaction field values (typed) or add transaction fields (ad-hoc)
    - In typed, need a way to traverse nested types
  - Add transaction associations
  - Close the transaction -- establishng end time

## Transaction-reading API
- Key application is supporting replay
- Support a default 'read' database ; optional per-name ones
- Enable finding target stream
- Enable walking transactions by start time

## Initialization / Configuration
- Static initialization
  - Use plusargs to control things like filename, and whether enabled
- Allow clients to check if recording is enabled

