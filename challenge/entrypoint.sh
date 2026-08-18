#!/bin/sh
# The instancer gives every instance its own port and passes it in as CHAL_PORT.
# xinetd wants it in its config file, so render the template before starting.
set -e
sed "s/__CHAL_PORT__/${CHAL_PORT:-41240}/" /etc/xinetd.d/chal.template > /etc/xinetd.d/chal
exec /usr/sbin/xinetd -dontfork
