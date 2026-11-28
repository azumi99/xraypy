-- Match the role grants on the existing warehouse settings data.
DO $$
DECLARE
    table_grant record;
BEGIN
    FOR table_grant IN
        SELECT pg_get_userbyid(acl.grantee) AS role_name,
               acl.privilege_type, acl.is_grantable
        FROM pg_class AS relation
        CROSS JOIN LATERAL aclexplode(relation.relacl) AS acl
        WHERE relation.oid = 'cv.label_print_cas'::regclass
          AND acl.grantee NOT IN (0, relation.relowner)
    LOOP
        EXECUTE format(
            'GRANT %s ON TABLE cv.xray_device TO %I%s',
            table_grant.privilege_type,
            table_grant.role_name,
            CASE WHEN table_grant.is_grantable THEN ' WITH GRANT OPTION' ELSE '' END
        );
    END LOOP;
END
$$;
