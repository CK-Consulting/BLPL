#!/usr/bin/perl
# Squid's helper for workspace egress (docs/architecture-v2.md, "Egress").
#
#   blpl-egress-helper.pl auth   basic authentication: "user password" -> OK/ERR
#   blpl-egress-helper.pl acl    external ACL: "user host port"         -> OK/ERR
#
# Each line Squid sends becomes one JSON question on the control plane's
# Unix socket (app/backend/app/egress_server.py), and its answer becomes OK
# or ERR. Perl because it is what the Squid image ships; core modules only.
#
# Fails closed: a socket that cannot be reached, a timeout, or anything but
# an exact "OK" from the control plane is ERR.
use strict;
use warnings;
use IO::Socket::UNIX;
use JSON::PP;

$| = 1;
my $mode = shift @ARGV // '';
my $sock_path = $ENV{BLPL_EGRESS_SOCKET} // '/run/blpl-egress/decide.sock';
die "usage: $0 auth|acl\n" unless $mode eq 'auth' || $mode eq 'acl';
my $json = JSON::PP->new->canonical;

sub unescape {
    my ($s) = @_;
    $s //= '';
    $s =~ s/%([0-9A-Fa-f]{2})/chr(hex($1))/ge;
    return $s;
}

sub ask {
    my ($question) = @_;
    my $answer = '';
    eval {
        local $SIG{ALRM} = sub { die "timeout\n" };
        alarm 5;
        my $s = IO::Socket::UNIX->new(Peer => $sock_path, Type => SOCK_STREAM)
            or die "connect: $!\n";
        print $s $json->encode($question), "\n";
        $answer = <$s> // '';
        close $s;
        alarm 0;
    };
    alarm 0;
    return ($answer =~ /\AOK\r?\n?\z/) ? 'OK' : 'ERR';
}

while (my $line = <STDIN>) {
    chomp $line;
    my @f = split / /, $line;
    my $reply = 'ERR';
    if ($mode eq 'auth' && @f >= 2) {
        $reply = ask({ op => 'auth', user => unescape($f[0]), password => unescape($f[1]) });
    } elsif ($mode eq 'acl' && @f >= 3) {
        $reply = ask({ op => 'acl', user => unescape($f[0]), host => unescape($f[1]),
                       port => unescape($f[2]) });
    }
    print "$reply\n";
}
