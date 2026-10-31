# NFR-0006. Limit the XML codec to flat models

## Attribute

Constraint / maintainability.

## Description

The XML codec shall only be required to support "flat" models —
scalar fields or flat lists of scalars — not nested models. This
bounds the codec's complexity deliberately rather than building a
general XML/object mapper.

## Source

Developers maintaining the template. Documented in
`src/app/xml_codec.py`.

## Verification

Code review; a resource with nested fields is out of scope for the
XML routes by design, not a bug to fix.
