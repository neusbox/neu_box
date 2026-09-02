%{!?neu_box_version:%global neu_box_version 0.0.0}
%{!?neu_box_release:%global neu_box_release 1}

Name:           neu-box-worker
Version:        %{neu_box_version}
Release:        %{neu_box_release}
Summary:        Neu Box accelerator worker
License:        MIT
URL:            https://github.com/neusbox/neu_box
Source0:        %{name}-%{version}-%{release}.tar.gz

ExclusiveArch:  x86_64 aarch64
Requires:       /bin/bash
Requires:       systemd

# PyInstaller resolves libraries below _internal itself. Do not advertise those
# private copies as system capabilities or turn their internal edges into host
# dependencies. Keep automatic discovery enabled for the top-level bootloader,
# native sandbox, and the rest of the package.
%global __provides_exclude_from ^%{_libexecdir}/neu-box/worker/_internal/.*\\.so.*$
%global __requires_exclude_from ^%{_libexecdir}/neu-box/worker/_internal/.*$

# The worker and sandbox are already-built release artifacts.  Keep this RPM
# as their owner instead of rebuilding them in a package scriptlet.
%global debug_package %{nil}
# GNU strip on some distributions treats the eBPF ELF object as an unknown
# architecture. All payloads are already finalized, so do not mutate them.
%global __strip /bin/true

%description
Neu Box Worker runs accelerator jobs and enforces their device isolation.
This package contains the self-contained worker bundle, native sandbox helper,
precompiled BPF object, device information scripts, configuration, and systemd
unit.

%prep
%autosetup -p1

%build
# All executable payloads are built before rpmbuild.

%install
rm -rf %{buildroot}
cp -a rootfs/. %{buildroot}/

test -x %{buildroot}%{_libexecdir}/neu-box/worker/neu-box-worker
test -x %{buildroot}%{_libexecdir}/neu-box/neu-box-sandbox
test -f %{buildroot}%{_libexecdir}/neu-box/device_block.o
test -x %{buildroot}%{_sbindir}/neu-box
test -L %{buildroot}%{_sbindir}/neu-box-worker
test -f %{buildroot}%{_unitdir}/neu-box-worker.service
test -f %{buildroot}%{_sysconfdir}/neu-box/worker.env

%pre
# Replacing an onedir PyInstaller bundle below a live process is unsafe: it can
# import another bundled module after RPM has changed the files.
if /usr/bin/systemctl is-active --quiet \
    neu-box-worker.service >/dev/null 2>&1; then
    echo "neu-box-worker.service is active; stop it before installing this RPM" >&2
    exit 1
fi

%post
# Deliberately do not enable or start the service. Database/config migration
# belongs to the deployment workflow, not an RPM scriptlet.
/usr/bin/systemctl daemon-reload >/dev/null 2>&1 || :

%preun
if [ "$1" -eq 0 ]; then
    if /usr/bin/systemctl is-active --quiet \
        neu-box-worker.service >/dev/null 2>&1; then
        echo "neu-box-worker.service is active; stop it before erasing this RPM" >&2
        exit 1
    fi
    /usr/bin/systemctl disable neu-box-worker.service >/dev/null 2>&1 || :
fi

%postun
if [ -x /usr/bin/systemctl ]; then
    /usr/bin/systemctl daemon-reload >/dev/null 2>&1 || :
fi

%files
%license LICENSE
%dir %{_libexecdir}/neu-box
%{_libexecdir}/neu-box/worker
%attr(0755,root,root) %{_libexecdir}/neu-box/neu-box-sandbox
%attr(0644,root,root) %{_libexecdir}/neu-box/device_block.o
%dir %{_datadir}/neu-box
%{_datadir}/neu-box/info
%attr(0755,root,root) %{_sbindir}/neu-box
%{_sbindir}/neu-box-worker
%attr(0644,root,root) %{_unitdir}/neu-box-worker.service
%config(noreplace) %attr(0640,root,root) %{_sysconfdir}/neu-box/worker.env
%dir %attr(0750,root,root) %{_localstatedir}/lib/neu-box/worker

%changelog
* Tue Sep 01 2026 Neu Box contributors <noreply@neu-box.local> - %{neu_box_version}-%{neu_box_release}
- Initial RPM packaging
