package Plugins::VibesboxTranscode::Plugin;

# Carrier plugin. It exists only so LMS scans this directory for
# custom-convert.conf — PluginManager->dirsFor('convert') returns the basedir of
# every ENABLED plugin, and a directory with no install.xml/module is not a
# plugin and never gets scanned. There is deliberately no runtime behaviour.

use strict;
use base qw(Slim::Plugin::Base);

sub initPlugin {
	my $class = shift;
	$class->SUPER::initPlugin(@_);
}

1;
