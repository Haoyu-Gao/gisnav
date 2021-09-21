from setuptools import setup
import xml.etree.ElementTree as ET

# Parse info from package.xml
tree = ET.parse('package.xml')
root = tree.getroot()
package_name = root.find('name').text
version = root.find('version').text
description = root.find('description').text
maintainer = root.find('maintainer').text
maintainer_email = root.find('maintainer').attrib.get('email', '')
license = root.find('license').text

setup(
    name=package_name,
    version=version,
    packages=[package_name],
    data_files=[
        ('share/ament_index/resource_index/packages',
            ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer=maintainer,
    maintainer_email=maintainer_email,
    description=description,
    license=license,
    tests_require=['pytest'],
    entry_points={
        'console_scripts': [
            'matching_node = wms_map_matching.matching_node:main'
        ],
    },
)
