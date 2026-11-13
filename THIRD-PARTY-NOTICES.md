# Third-party notices

`hardcharger.metabase-driver.jar` contains this driver and, bundled unchanged from Maven Central:

| Component | Version | License |
|---|---|---|
| [Hardcharger JDBC driver](https://github.com/gizmodata/hardcharger-jdbc) (`com.gizmodata:hardcharger-jdbc`) | 2.4.4 | Apache-2.0, Copyright 2026 GizmoData LLC |
| [Chargewire JDBC driver](https://github.com/gizmodata/chargewire-jdbc) (`com.gizmodata:chargewire-jdbc`) | 2.2.6 | Apache-2.0, Copyright 2026 GizmoData LLC |

The Chargewire JDBC driver is a single jar that carries its own dependencies (relocated under
`com.gizmodata.chargewire.jdbc.shaded`, except zstd-jni, whose native libraries need its own package
name). Their versions below are read from the `META-INF/maven/*/pom.properties` entries in that jar:

| Component | Version | License |
|---|---|---|
| Apache Arrow Java (`arrow-vector`, `arrow-format`, `arrow-memory-core`, `arrow-memory-unsafe`, `arrow-compression`) | 19.0.0 | Apache-2.0 |
| Jackson (`jackson-core`, `jackson-databind`, `jackson-datatype-jsr310` 2.22.3; `jackson-annotations` 2.22) | 2.22 | Apache-2.0 |
| FlatBuffers Java (`flatbuffers-java`) | 25.2.10 | Apache-2.0 |
| Apache Commons Codec | 1.23.0 | Apache-2.0 |
| Apache Commons Compress | 1.28.0 | Apache-2.0 |
| Apache Commons IO | 2.20.0 | Apache-2.0 |
| Apache Commons Lang | 3.18.0 | Apache-2.0 |
| SLF4J (`slf4j-api`, `slf4j-nop`) | 2.0.20 | MIT |
| zstd-jni (`com.github.luben:zstd-jni`), with the Zstandard library in its native libraries | 1.5.7-23 | BSD-2-Clause (zstd-jni), BSD-3-Clause (Zstandard) |
| FastDoubleParser (inside jackson-core) | as bundled by jackson-core 2.22.3 | MIT |
| Schubfach (inside jackson-core) | as bundled by jackson-core 2.22.3 | MIT |

The Apache License 2.0 is in `LICENSE`. The attribution notices of the Apache-licensed components
are in the jar at `META-INF/NOTICE` (the Chargewire JDBC driver's NOTICE file), and the
FastDoubleParser and Schubfach licenses at `META-INF/FastDoubleParser-LICENSE`,
`META-INF/FastDoubleParser-ThirdParty-LICENSE` and `META-INF/Schubfach-LICENSE`. The licenses the
Chargewire JDBC jar does not carry itself follow.

## zstd-jni

```
Copyright (c) 2015-present, Luben Karavelov/ All rights reserved.

BSD-2-Clause License https://opensource.org/license/bsd-2-clause

Redistribution and use in source and binary forms, with or without modification,
are permitted provided that the following conditions are met:

* Redistributions of source code must retain the above copyright notice, this
  list of conditions and the following disclaimer.

* Redistributions in binary form must reproduce the above copyright notice, this
  list of conditions and the following disclaimer in the documentation and/or
  other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR
ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
(INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON
ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

## Zstandard

```
BSD License

For Zstandard software

Copyright (c) Meta Platforms, Inc. and affiliates. All rights reserved.

Redistribution and use in source and binary forms, with or without modification,
are permitted provided that the following conditions are met:

 * Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.

 * Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

 * Neither the name Facebook, nor Meta, nor the names of its contributors may
   be used to endorse or promote products derived from this software without
   specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR
ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
(INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON
ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

## SLF4J

```
Copyright (c) 2004-2022 QOS.ch Sarl (Switzerland)
All rights reserved.

Permission is hereby granted, free  of charge, to any person obtaining
a  copy  of this  software  and  associated  documentation files  (the
"Software"), to  deal in  the Software without  restriction, including
without limitation  the rights to  use, copy, modify,  merge, publish,
distribute,  sublicense, and/or sell  copies of  the Software,  and to
permit persons to whom the Software  is furnished to do so, subject to
the following conditions:

The  above  copyright  notice  and  this permission  notice  shall  be
included in all copies or substantial portions of the Software.

THE  SOFTWARE IS  PROVIDED  "AS  IS", WITHOUT  WARRANTY  OF ANY  KIND,
EXPRESS OR  IMPLIED, INCLUDING  BUT NOT LIMITED  TO THE  WARRANTIES OF
MERCHANTABILITY,    FITNESS    FOR    A   PARTICULAR    PURPOSE    AND
NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE
LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
OF CONTRACT, TORT OR OTHERWISE,  ARISING FROM, OUT OF OR IN CONNECTION
WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
```
