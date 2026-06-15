import pytest

from app.services.route_ingestion import RouteIngestionError, RouteIngestionService


def test_parse_single_linestring_kml() -> None:
    content = """
    <kml xmlns="http://www.opengis.net/kml/2.2">
      <Document>
        <Placemark>
          <name>武功山穿越</name>
          <description>龙山村到金顶</description>
          <LineString>
            <coordinates>
              114.10,27.45,800 114.11,27.46,1200 114.12,27.47,1000
            </coordinates>
          </LineString>
        </Placemark>
      </Document>
    </kml>
    """

    routes = RouteIngestionService().parse_kml(content)

    assert len(routes) == 1
    assert routes[0].name == "武功山穿越"
    assert routes[0].description == "龙山村到金顶"
    assert routes[0].coordinates[1].elevation_m == 1200
    assert routes[0].source == "user_kml"


def test_parse_multiple_placemarks_and_missing_name() -> None:
    content = """
    <kml>
      <Document>
        <Placemark>
          <LineString><coordinates>1,1 1.1,1.1</coordinates></LineString>
        </Placemark>
        <Placemark>
          <name>第二段</name>
          <LineString><coordinates>2,2,10 2.1,2.1,20</coordinates></LineString>
        </Placemark>
      </Document>
    </kml>
    """

    routes = RouteIngestionService().parse_kml(content)

    assert [route.name for route in routes] == ["KML Route 1", "第二段"]


def test_empty_kml_raises() -> None:
    with pytest.raises(RouteIngestionError, match="empty"):
        RouteIngestionService().parse_kml("   ")


def test_invalid_xml_raises() -> None:
    with pytest.raises(RouteIngestionError, match="Invalid KML XML"):
        RouteIngestionService().parse_kml("<kml>")


def test_kml_without_linestring_raises() -> None:
    with pytest.raises(RouteIngestionError, match="LineString / gx:Track"):
        RouteIngestionService().parse_kml("<kml><Placemark><Point /></Placemark></kml>")
